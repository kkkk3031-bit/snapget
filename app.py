from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import os, uuid, json, re, glob, threading, time, shutil, subprocess
import requests as req_lib
from collections import defaultdict

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/snap_cache'
FILE_TTL     = 1800
RATE_LIMIT   = 10

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def _find_ytdlp():
    p = shutil.which('yt-dlp') or shutil.which('yt_dlp')
    if p:
        return p
    for d in os.environ.get('PATH', '').split(':'):
        for name in ('yt-dlp', 'yt_dlp'):
            fp = os.path.join(d, name)
            if os.path.isfile(fp):
                return fp
    return 'yt-dlp'

jobs        = {}
jobs_lock   = threading.Lock()
_rate_store = defaultdict(list)
_rate_lock  = threading.Lock()

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}


# ── Job persistence ───────────────────────────────────────────────────────────

def _job_path(job_id):
    return os.path.join(DOWNLOAD_DIR, f'job_{job_id}.json')

def _save_job(job_id, job):
    try:
        with open(_job_path(job_id), 'w') as f:
            json.dump(job, f)
    except Exception:
        pass

def _load_job_from_disk(job_id):
    try:
        with open(_job_path(job_id)) as f:
            return json.load(f)
    except Exception:
        return None

def _load_all_jobs():
    for p in glob.glob(os.path.join(DOWNLOAD_DIR, 'job_*.json')):
        try:
            with open(p) as f:
                job = json.load(f)
            job_id = os.path.basename(p)[4:-5]
            if job.get('status') in ('pending', 'processing'):
                job['status'] = 'error'
                job['error']  = 'Server restarted. Please try again.'
                _save_job(job_id, job)
            if job.get('status') == 'done' and not os.path.exists(job.get('file', '')):
                os.remove(p); continue
            jobs[job_id] = job
        except Exception:
            pass

_load_all_jobs()


# ── URL helpers ───────────────────────────────────────────────────────────────
# Snapchat public URLs:
#   snapchat.com/spotlight/<id>
#   snapchat.com/p/<userhash>/<storyid>
#   snapchat.com/t/<shortcode>            (shortlink redirects)
#   story.snapchat.com/s/<id>
#   snapchat.com/add/<username>           (profile)

_SNAP_RE = re.compile(
    r'(?:^|[/.])(?:snapchat\.com|story\.snapchat\.com)/'
    r'(?:spotlight|p|t|s|add)/[\w\-./]+',
    re.IGNORECASE)

def is_valid_url(url):
    return bool(_SNAP_RE.search(url))

def normalize_url(url):
    url = url.strip()
    if not url.startswith('http'):
        url = 'https://' + url
    return url

def make_filename(title, ext='mp4'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#@]', '', title or 'snap').strip()
    name = re.sub(r'\s+', ' ', name)
    return (name[:80] or 'snap') + '.' + ext

def short_id_from_url(url):
    m = re.search(r'(?:spotlight|p|t|s)/([\w\-]+)', url)
    return (m.group(1)[-10:] if m else 'snap')

def _find_ffmpeg():
    p = shutil.which('ffmpeg')
    if p: return p
    for d in ['/nix/var/nix/profiles/default/bin', '/usr/bin', '/usr/local/bin']:
        fp = os.path.join(d, 'ffmpeg')
        if os.path.isfile(fp): return fp
    nix = glob.glob('/nix/store/*/bin/ffmpeg')
    return nix[0] if nix else None


# ── yt-dlp metadata + download ────────────────────────────────────────────────

def ytdlp_info(url):
    """Fetch metadata only — no download. Returns (info_dict, error_str)."""
    ytdlp = _find_ytdlp()
    cmd = [ytdlp, '--no-warnings', '--no-playlist', '--skip-download',
           '--dump-single-json', '--geo-bypass', url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            err = (proc.stderr or '').strip().splitlines()[-1:] or ['Could not fetch snap info.']
            msg = err[0]
            if 'private' in msg.lower() or 'not available' in msg.lower():
                return None, 'This snap is private or no longer available.'
            if 'unsupported' in msg.lower():
                return None, 'This Snapchat URL is not supported — try a public Spotlight or Story link.'
            return None, 'Could not fetch snap. Make sure the link is public.'
        data = json.loads(proc.stdout)
        return data, None
    except subprocess.TimeoutExpired:
        return None, 'Snapchat is responding slowly. Please try again.'
    except Exception:
        return None, 'Could not parse snap info.'

def ytdlp_download(url, output_template, job_id):
    ytdlp = _find_ytdlp()
    cmd = [ytdlp, '--geo-bypass', '--no-playlist', '--newline',
           '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
           '--merge-output-format', 'mp4',
           '-o', output_template, url]
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        cmd += ['--ffmpeg-location', os.path.dirname(ffmpeg)]
    _PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%')
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stderr:
            m = _PROGRESS_RE.search(line)
            if m:
                pct = min(int(float(m.group(1))), 90)
                with jobs_lock:
                    if jobs.get(job_id, {}).get('status') == 'processing':
                        jobs[job_id]['progress'] = pct
        proc.wait(timeout=300)
        if proc.returncode != 0:
            return None
        files = glob.glob(output_template.replace('%(ext)s', '*'))
        return files[0] if files else None
    except Exception:
        return None


# ── Worker ────────────────────────────────────────────────────────────────────

def _set_job(job_id, updates):
    with jobs_lock:
        jobs[job_id].update(updates)
        _save_job(job_id, jobs[job_id])

def schedule_cleanup(job_id, path):
    def _cleanup():
        time.sleep(FILE_TTL)
        try:
            if os.path.isfile(path):  os.remove(path)
            elif os.path.isdir(path): shutil.rmtree(path, ignore_errors=True)
        except Exception: pass
        try: os.remove(_job_path(job_id))
        except Exception: pass
        with jobs_lock: jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()

def do_download(job_id, url, title, fmt):
    _set_job(job_id, {'status': 'processing', 'progress': 5})
    try:
        file_id  = str(uuid.uuid4())
        out_tmpl = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')
        _set_job(job_id, {'progress': 15})

        dl_path = ytdlp_download(url, out_tmpl, job_id)
        if not dl_path or not os.path.exists(dl_path):
            _set_job(job_id, {'status': 'error', 'error': 'Could not download this snap. It may be private or region-blocked.'})
            return

        _set_job(job_id, {'progress': 92})

        sid = short_id_from_url(url)
        t   = (title or 'snap').strip()
        base_title = f'{t}_{sid}' if sid not in t else t

        if fmt == 'mp3':
            mp3_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            ffmpeg = _find_ffmpeg()
            if ffmpeg:
                subprocess.run([ffmpeg, '-i', dl_path, '-q:a', '0', '-map', 'a',
                                mp3_path, '-y'], capture_output=True, timeout=120)
                if os.path.exists(mp3_path):
                    try: os.remove(dl_path)
                    except Exception: pass
                    dl_path = mp3_path

        real_ext = os.path.splitext(dl_path)[1].lstrip('.') or 'mp4'
        filename = make_filename(base_title, real_ext)
        _set_job(job_id, {'status': 'done', 'file': dl_path,
                          'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, dl_path)

    except Exception:
        _set_job(job_id, {'status': 'error', 'error': 'Download failed. Please try again.'})


# ── Rate limiter ──────────────────────────────────────────────────────────────

def _check_rate(ip):
    now = time.time()
    with _rate_lock:
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
        if len(_rate_store[ip]) >= RATE_LIMIT: return False
        _rate_store[ip].append(now)
        return True

def _client_ip():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr or 'unknown')


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manifest.json')
def manifest():
    return jsonify({"name":"SnapGet","short_name":"SnapGet",
                    "description":"Download Snapchat Spotlight and Story videos",
                    "start_url":"/","display":"standalone",
                    "background_color":"#000000","theme_color":"#FFFC00","icons":[]})

@app.route('/robots.txt')
def robots():
    body = ('User-agent: *\n'
            'Allow: /\n\n'
            'Sitemap: https://snapget-production.up.railway.app/sitemap.xml\n')
    return body, 200, {'Content-Type': 'text/plain'}

@app.route('/sitemap.xml')
def sitemap():
    today = time.strftime('%Y-%m-%d')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           '  <url>\n'
           '    <loc>https://snapget-production.up.railway.app/</loc>\n'
           f'    <lastmod>{today}</lastmod>\n'
           '    <changefreq>weekly</changefreq>\n'
           '    <priority>1.0</priority>\n'
           '  </url>\n'
           '</urlset>\n')
    return xml, 200, {'Content-Type': 'application/xml'}

@app.route('/ads.txt')
def ads_txt():
    return 'google.com, pub-3956390078338144, DIRECT, f08c47fec0942fa0\n', 200, {'Content-Type': 'text/plain'}

@app.route('/info', methods=['POST'])
def get_info():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url  = normalize_url(data.get('url', '').strip())
    if not url or not is_valid_url(url):
        return jsonify({'error': 'Invalid Snapchat URL — paste a Spotlight or Story link.'}), 400

    info, err = ytdlp_info(url)
    if err or not info:
        return jsonify({'error': err or 'Could not fetch snap.'}), 400

    title    = info.get('title') or info.get('description') or 'Snapchat video'
    uploader = info.get('uploader') or info.get('uploader_id') or info.get('channel') or ''
    thumb    = info.get('thumbnail') or ''
    dur      = info.get('duration') or 0
    mins, secs = divmod(int(dur), 60) if dur else (0, 0)
    dur_str = f'{mins}:{secs:02d}' if dur else '—'

    return jsonify({
        'title':        str(title)[:200],
        'thumbnail':    thumb,
        'uploader':     str(uploader),
        'is_video':     True,
        'duration':     dur_str,
        'duration_sec': dur,
        'url':          url,
    })

@app.route('/start', methods=['POST'])
def start_convert():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data  = request.get_json() or {}
    url   = normalize_url(data.get('url', '').strip())
    title = data.get('title', '').strip()
    fmt   = data.get('format', 'mp4')
    if fmt not in ('mp4', 'mp3'): fmt = 'mp4'
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid Snapchat URL'}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None,
                         'error': None, 'progress': 0}
        _save_job(job_id, jobs[job_id])

    threading.Thread(target=do_download,
                     args=(job_id, url, title or None, fmt),
                     daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
        if job:
            with jobs_lock: jobs[job_id] = job
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k) for k in ('status', 'error', 'filename', 'progress')})

@app.route('/download/<job_id>')
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
        if job:
            with jobs_lock: jobs[job_id] = job
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please try again.'}), 404
    path, filename = job['file'], job['filename']
    if not os.path.exists(path):
        return jsonify({'error': 'File expired. Please download again.'}), 410
    safe = re.sub(r'[^\w\s\-\.\(\)]', '', filename).strip() or 'snap.mp4'
    if safe.endswith('.mp3'):   mime = 'audio/mpeg'
    elif safe.endswith('.jpg'): mime = 'image/jpeg'
    else:                       mime = 'video/mp4'
    return send_file(path, as_attachment=True, download_name=safe, mimetype=mime)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
