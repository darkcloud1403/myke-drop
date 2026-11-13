import os, sys, json, time, shutil, socket, mimetypes, io, secrets
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from http import cookies

# Optional Google Drive dependencies
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

# Auto-load .env or .env.txt if present
for _env_file in [".env", ".env.txt"]:
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _env_file)
    if os.path.isfile(_env_path):
        try:
            with open(_env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
                        if _k and _k not in os.environ:
                            os.environ[_k] = _v
        except Exception: pass

# Configuration & Auth
DROP_USER = os.environ.get("DROP_USER", "admin")
DROP_PASS = os.environ.get("DROP_PASS", "admin123")
PORT = int(os.environ.get("PORT", 8000))

# Active sessions: token -> expiry_timestamp
SESSIONS = {}

def safe_name(n):
    return os.path.basename(n).strip().lstrip(".").replace("/", "").replace("\\", "")

# ----------------- STORAGE: LOCAL -----------------
class LocalStorage:
    mode = "local"

    def __init__(self, root_dir):
        self.root = root_dir
        os.makedirs(os.path.join(self.root, "General"), exist_ok=True)

    def folder_dir(self, name):
        f = safe_name(name) or "General"
        path = os.path.join(self.root, f)
        os.makedirs(path, exist_ok=True)
        return path

    def list_folders(self):
        folders = []
        for d in sorted(os.listdir(self.root)):
            fp = os.path.join(self.root, d)
            if os.path.isdir(fp):
                count = len([x for x in os.listdir(fp) if not x.startswith(".")])
                folders.append({"name": d, "count": count})
        if not any(f["name"] == "General" for f in folders):
            folders.insert(0, {"name": "General", "count": 0})
        return folders

    def list_items(self, folder):
        d = self.folder_dir(folder)
        items = []
        for n in os.listdir(d):
            if n.startswith("."): continue
            fp = os.path.join(d, n)
            if not os.path.isfile(fp): continue
            s = os.stat(fp)
            it = {"name": n, "size": s.st_size, "time": s.st_mtime}
            if n.startswith("note-") and n.endswith(".txt"):
                try:
                    with open(fp, "r", encoding="utf8", errors="replace") as fh:
                        it["text"] = fh.read()
                except Exception:
                    it["text"] = ""
            items.append(it)
        return sorted(items, key=lambda x: x["time"])

    def get_file(self, folder, name):
        fp = os.path.join(self.folder_dir(folder), safe_name(name))
        if not os.path.isfile(fp):
            return None, None, 0
        ct = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        sz = os.path.getsize(fp)
        return open(fp, "rb"), ct, sz

    def upload_file(self, folder, name, stream, length):
        d = self.folder_dir(folder)
        fname = safe_name(name) or "file"
        base, ext = os.path.splitext(fname)
        fp = os.path.join(d, fname)
        i = 1
        while os.path.exists(fp):
            fp = os.path.join(d, f"{base} ({i}){ext}")
            i += 1
        with open(fp, "wb") as fh:
            rem = length
            while rem > 0:
                chunk = stream.read(min(rem, 1 << 20))
                if not chunk: break
                fh.write(chunk)
                rem -= len(chunk)
        return os.path.basename(fp)

    def save_note(self, folder, body):
        d = self.folder_dir(folder)
        fname = f"note-{int(time.time() * 1000)}.txt"
        with open(os.path.join(d, fname), "wb") as fh:
            fh.write(body)

    def create_folder(self, name):
        n = safe_name(name)
        if not n: return False
        os.makedirs(os.path.join(self.root, n), exist_ok=True)
        return True

    def delete_item(self, folder, name):
        fp = os.path.join(self.folder_dir(folder), safe_name(name))
        if os.path.isfile(fp):
            os.remove(fp)

    def delete_folder(self, folder):
        f = safe_name(folder)
        if f and f != "General":
            shutil.rmtree(self.folder_dir(f), ignore_errors=True)

    def move_item(self, name, from_folder, to_folder):
        src = os.path.join(self.folder_dir(from_folder), safe_name(name))
        dst = os.path.join(self.folder_dir(to_folder), safe_name(name))
        if os.path.exists(src):
            os.replace(src, dst)

# ----------------- STORAGE: GOOGLE DRIVE -----------------
class GoogleDriveStorage:
    mode = "gdrive"

    def __init__(self, creds, root_folder_id):
        self.creds = creds
        self.root_id = root_folder_id
        self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        self.folders_cache = {}
        self.ensure_folder("General")

    def test_connection(self):
        f = self.service.files().get(fileId=self.root_id, fields="id, name").execute()
        return f.get("name")

    def get_folder_id(self, name, create=False):
        f = safe_name(name) or "General"
        if f in self.folders_cache:
            return self.folders_cache[f]
        q = f"'{self.root_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and name = '{f}' and trashed = false"
        res = self.service.files().list(q=q, fields="files(id, name)").execute()
        files = res.get("files", [])
        if files:
            self.folders_cache[f] = files[0]["id"]
            return files[0]["id"]
        if create:
            meta = {'name': f, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.root_id]}
            created = self.service.files().create(body=meta, fields='id').execute()
            self.folders_cache[f] = created["id"]
            return created["id"]
        return None

    def ensure_folder(self, name):
        return self.get_folder_id(name, create=True)

    def list_folders(self):
        q = f"'{self.root_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = self.service.files().list(q=q, fields="files(id, name)").execute()
        raw_folders = res.get("files", [])
        
        result = []
        found_gen = False
        for rf in raw_folders:
            fid = rf["id"]; fname = rf["name"]
            self.folders_cache[fname] = fid
            if fname == "General": found_gen = True
            # Count items inside
            iq = f"'{fid}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
            ires = self.service.files().list(q=iq, fields="files(id)").execute()
            count = len(ires.get("files", []))
            result.append({"name": fname, "count": count})
            
        if not found_gen:
            gid = self.ensure_folder("General")
            result.insert(0, {"name": "General", "count": 0})
        return sorted(result, key=lambda x: (x["name"] != "General", x["name"]))

    def list_items(self, folder):
        fid = self.get_folder_id(folder, create=False)
        if not fid: return []
        q = f"'{fid}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        res = self.service.files().list(q=q, fields="files(id, name, size, createdTime, description)").execute()
        items = []
        for f in res.get("files", []):
            fname = f["name"]
            size = int(f.get("size", 0))
            # Parse createdTime ISO format to timestamp
            try:
                t_str = f.get("createdTime", "").replace("Z", "+00:00")
                import datetime
                dt = datetime.datetime.fromisoformat(t_str)
                mtime = dt.timestamp()
            except Exception:
                mtime = time.time()
            it = {"name": fname, "size": size, "time": mtime, "id": f["id"]}
            if fname.startswith("note-") and fname.endswith(".txt"):
                # Retrieve short note content
                try:
                    desc = f.get("description")
                    if desc:
                        it["text"] = desc
                    else:
                        req = self.service.files().get_media(fileId=f["id"])
                        fh = io.BytesIO()
                        dl = MediaIoBaseDownload(fh, req)
                        done = False
                        while not done: _, done = dl.next_chunk()
                        fh.seek(0)
                        it["text"] = fh.read().decode("utf8", errors="replace")
                except Exception:
                    it["text"] = ""
            items.append(it)
        return sorted(items, key=lambda x: x["time"])

    def get_file(self, folder, name):
        fid = self.get_folder_id(folder, create=False)
        if not fid: return None, None, 0
        fname = safe_name(name)
        q = f"'{fid}' in parents and name = '{fname}' and trashed = false"
        res = self.service.files().list(q=q, fields="files(id, name, size, mimeType)").execute()
        files = res.get("files", [])
        if not files: return None, None, 0
        target = files[0]
        req = self.service.files().get_media(fileId=target["id"])
        fh = io.BytesIO()
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done: _, done = dl.next_chunk()
        fh.seek(0)
        ct = target.get("mimeType") or mimetypes.guess_type(fname)[0] or "application/octet-stream"
        sz = int(target.get("size", len(fh.getbuffer())))
        return fh, ct, sz

    def upload_file(self, folder, name, stream, length):
        fid = self.ensure_folder(folder)
        fname = safe_name(name) or "file"
        data = stream.read(length)
        ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=ct, resumable=False)
        meta = {'name': fname, 'parents': [fid]}
        f = self.service.files().create(body=meta, media_body=media, fields='id, name').execute()
        return f.get("name")

    def save_note(self, folder, body):
        fid = self.ensure_folder(folder)
        fname = f"note-{int(time.time() * 1000)}.txt"
        text = body.decode("utf8", errors="replace")
        media = MediaIoBaseUpload(io.BytesIO(body), mimetype="text/plain", resumable=False)
        meta = {'name': fname, 'parents': [fid], 'description': text}
        self.service.files().create(body=meta, media_body=media, fields='id').execute()

    def create_folder(self, name):
        f = safe_name(name)
        if not f: return False
        self.ensure_folder(f)
        return True

    def delete_item(self, folder, name):
        fid = self.get_folder_id(folder, create=False)
        if not fid: return
        fname = safe_name(name)
        q = f"'{fid}' in parents and name = '{fname}' and trashed = false"
        res = self.service.files().list(q=q, fields="files(id)").execute()
        for f in res.get("files", []):
            try: self.service.files().delete(fileId=f["id"]).execute()
            except Exception: pass

    def delete_folder(self, folder):
        f = safe_name(folder)
        if not f or f == "General": return
        fid = self.get_folder_id(f, create=False)
        if fid:
            try:
                self.service.files().delete(fileId=fid).execute()
                self.folders_cache.pop(f, None)
            except Exception: pass

    def move_item(self, name, from_folder, to_folder):
        from_fid = self.get_folder_id(from_folder, create=False)
        to_fid = self.ensure_folder(to_folder)
        if not from_fid or not to_fid: return
        fname = safe_name(name)
        q = f"'{from_fid}' in parents and name = '{fname}' and trashed = false"
        res = self.service.files().list(q=q, fields="files(id)").execute()
        if res.get("files"):
            file_id = res["files"][0]["id"]
            self.service.files().update(fileId=file_id, addParents=to_fid, removeParents=from_fid).execute()

# ----------------- STORAGE INITIALIZATION -----------------
def init_storage():
    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    sa_file = os.environ.get("GDRIVE_SERVICE_ACCOUNT_FILE", os.path.join(os.path.dirname(__file__), "service_account.json"))
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")

    if GDRIVE_AVAILABLE and folder_id and (sa_json or os.path.exists(sa_file)):
        try:
            if sa_json:
                info = json.loads(sa_json)
                creds = service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/drive'])
            else:
                creds = service_account.Credentials.from_service_account_file(sa_file, scopes=['https://www.googleapis.com/auth/drive'])
            store = GoogleDriveStorage(creds, folder_id.strip())
            name = store.test_connection()
            print(f"[Storage] Connected to Google Drive folder '{name}' ({folder_id}) successfully!")
            return store
        except Exception as e:
            print(f"[Storage] Warning: Failed to connect to Google Drive ({e}). Falling back to local vault.")
    
    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault")
    print(f"[Storage] Running in LOCAL mode: {local_dir}")
    return LocalStorage(local_dir)

storage = init_storage()

# ----------------- HTTP HANDLER -----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def out(self, body=b"{}", code=200, ct="application/json", headers=None):
        if not isinstance(body, bytes):
            body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def get_auth_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            c = cookies.SimpleCookie()
            c.load(cookie_header)
            if "session_token" in c:
                return c["session_token"].value
        return None

    def is_authenticated(self):
        token = self.get_auth_token()
        if not token: return False
        exp = SESSIONS.get(token)
        if exp and exp > time.time():
            return True
        if token in SESSIONS:
            del SESSIONS[token]
        return False

    def do_GET(self):
        u = urlparse(self.path); p = u.path
        a = {k: v[0] for k, v in parse_qs(u.query).items()}

        if p == "/":
            return self.out(PAGE, ct="text/html; charset=utf-8")
        if p == "/manifest.json":
            return self.out({"name": "Drop", "short_name": "Drop", "start_url": "/", "display": "standalone",
                             "background_color": "#101820", "theme_color": "#101820"}, ct="application/manifest+json")

        if p == "/api/status":
            return self.out({"authenticated": self.is_authenticated(), "storage": storage.mode})

        # All other endpoints require authentication
        if not self.is_authenticated():
            return self.out({"error": "Unauthorized"}, code=401)

        if p == "/api/folders":
            try:
                return self.out(storage.list_folders())
            except Exception as e:
                return self.out({"error": str(e)}, code=500)

        if p == "/api/items":
            try:
                folder = a.get("f", "General")
                return self.out(storage.list_items(folder))
            except Exception as e:
                return self.out({"error": str(e)}, code=500)

        if p.startswith("/files/"):
            try:
                parts = p.split("/", 3)
                if len(parts) < 4: return self.out(code=404)
                _, _, f, n = parts
                folder = unquote(f); name = unquote(n)
                stream, ct, sz = storage.get_file(folder, name)
                if not stream: return self.out(code=404)
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(sz))
                self.end_headers()
                shutil.copyfileobj(stream, self.wfile)
                if hasattr(stream, "close"): stream.close()
                return
            except Exception:
                return self.out(code=500)

        self.out(code=404)

    def do_POST(self):
        u = urlparse(self.path); p = u.path
        a = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length", 0))

        if p == "/api/login":
            body = self.rfile.read(n)
            try:
                data = json.loads(body.decode())
                user = data.get("username", "")
                pwd = data.get("password", "")
                if secrets.compare_digest(user, DROP_USER) and secrets.compare_digest(pwd, DROP_PASS):
                    token = secrets.token_hex(24)
                    SESSIONS[token] = time.time() + (30 * 86400) # 30 days
                    cookie = f"session_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                    return self.out({"ok": True, "token": token, "storage": storage.mode}, headers={"Set-Cookie": cookie})
                return self.out({"error": "Invalid username or password"}, code=401)
            except Exception as e:
                return self.out({"error": str(e)}, code=400)

        if p == "/api/logout":
            token = self.get_auth_token()
            if token in SESSIONS: del SESSIONS[token]
            cookie = "session_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            return self.out({"ok": True}, headers={"Set-Cookie": cookie})

        # Require authentication for remaining POST endpoints
        if not self.is_authenticated():
            return self.out({"error": "Unauthorized"}, code=401)

        if p == "/api/upload":
            folder = a.get("f", "General"); name = a.get("name", "file")
            try:
                storage.upload_file(folder, name, self.rfile, n)
                return self.out({"ok": True})
            except Exception as e:
                return self.out({"error": str(e)}, code=500)

        body = self.rfile.read(n)
        if p == "/api/note":
            try:
                storage.save_note(a.get("f", "General"), body)
                return self.out({"ok": True})
            except Exception as e:
                return self.out({"error": str(e)}, code=500)

        elif p == "/api/folder":
            try:
                name = safe_name(body.decode())
                if not name: return self.out(code=400)
                storage.create_folder(name)
                return self.out({"ok": True})
            except Exception as e:
                return self.out({"error": str(e)}, code=500)

        else:
            j = json.loads(body or b"{}")
            f = j.get("f", "General")
            try:
                if p == "/api/move":
                    storage.move_item(j["name"], f, j["to"])
                elif p == "/api/delete":
                    if "name" in j:
                        storage.delete_item(f, j["name"])
                    else:
                        storage.delete_folder(f)
                return self.out({"ok": True})
            except Exception as e:
                return self.out({"error": str(e)}, code=500)

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#101820"><link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes"><title>Drop</title>
<style>
:root{--bg:#eef1f4;--side:#fff;--bub:#d8f1e6;--card:rgba(120,130,140,.14);--tx:#14202b;--mu:#6b7886;--ln:#dde3e9;--ac:#0f9d78;--hd:#fff;--danger:#dc2626}
@media(prefers-color-scheme:dark){:root{--bg:#0c1319;--side:#101820;--bub:#0d4a3b;--card:rgba(255,255,255,.08);--tx:#e6edf3;--mu:#8795a3;--ln:#1f2b36;--ac:#22c597;--hd:#131d26;--danger:#ef4444}}
*{box-sizing:border-box;margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
html,body{height:100%}
body{background:var(--bg);color:var(--tx);font-size:15px;line-height:1.4;display:flex;overflow:hidden}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
aside{width:290px;flex:none;background:var(--side);border-right:1px solid var(--ln);display:flex;flex-direction:column;padding-top:env(safe-area-inset-top)}
aside h1{font-size:20px;padding:18px 18px 10px;letter-spacing:-.3px;display:flex;align-items:center;justify-content:space-between}
.badge{font-size:11px;font-weight:600;padding:2px 7px;border-radius:12px;background:var(--card);color:var(--ac)}
#fl{flex:1;overflow:auto;padding:4px 8px}
.f{display:flex;justify-content:space-between;width:100%;padding:11px 12px;border-radius:10px;text-align:left}
.f:hover{background:var(--card)}.f.on{background:var(--bub);font-weight:600}.f small{color:var(--mu)}
.add{margin:8px;padding:11px;border-radius:10px;border:1px dashed var(--ln);color:var(--ac);font-weight:600}
main{flex:1;min-width:0;display:flex;flex-direction:column;height:100dvh}
header{display:flex;align-items:center;gap:10px;background:var(--hd);border-bottom:1px solid var(--ln);padding:12px 14px;padding-top:calc(12px + env(safe-area-inset-top))}
header h2{font-size:17px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
header button{padding:6px 8px;border-radius:8px;color:var(--mu)}header button:hover{background:var(--card)}
#menu{display:none}
#feed{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:8px}
.m{align-self:flex-end;max-width:min(82%,440px);background:var(--bub);border-radius:14px 14px 4px 14px;padding:8px 10px}
.t{white-space:pre-wrap;word-break:break-word}
.m img{display:block;max-width:100%;max-height:320px;border-radius:8px}
.file{display:flex;gap:10px;align-items:center;text-decoration:none;color:inherit;background:var(--card);border-radius:10px;padding:8px 10px}
.ic{flex:none;width:42px;height:42px;border-radius:8px;background:var(--ac);color:#fff;display:grid;place-items:center;font-size:11px;font-weight:700}
.file b{display:block;word-break:break-all;font-weight:600}.file small{color:var(--mu)}
.meta{font-size:11.5px;color:var(--mu);margin-top:5px;text-align:right}
.meta a{cursor:pointer;text-decoration:underline}
.empty{margin:auto;color:var(--mu);text-align:center;max-width:260px}
#bar{display:flex;gap:8px;align-items:flex-end;padding:10px;padding-bottom:calc(10px + env(safe-area-inset-bottom));background:var(--hd);border-top:1px solid var(--ln)}
#in{flex:1;resize:none;max-height:130px;padding:10px 14px;border-radius:20px;border:1px solid var(--ln);background:var(--side);color:var(--tx);font-size:15px;outline:0}
#in:focus{border-color:var(--ac)}
.rb{flex:none;width:42px;height:42px;border-radius:50%;display:grid;place-items:center}
#at{color:var(--mu)}#at:hover{background:var(--card)}#sd{background:var(--ac);color:#fff}
svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
#toast{position:fixed;left:50%;bottom:80px;transform:translateX(-50%);background:#14202b;color:#fff;padding:8px 16px;border-radius:20px;font-size:13px;display:none;z-index:99}
dialog{margin:auto;border:0;border-radius:16px;padding:16px;width:min(320px,90vw);background:var(--side);color:var(--tx)}
dialog::backdrop{background:rgba(0,0,0,.5)}dialog h3{margin-bottom:8px;font-size:16px}
.drag #feed{outline:2px dashed var(--ac);outline-offset:-8px}
/* Auth View */
#auth-view{display:none;position:fixed;inset:0;background:var(--bg);z-index:100;align-items:center;justify-content:center;padding:20px}
.auth-card{background:var(--side);border:1px solid var(--ln);border-radius:18px;padding:28px 24px;width:min(340px,94vw);box-shadow:0 8px 30px rgba(0,0,0,.08);text-align:center}
.auth-card h1{font-size:22px;margin-bottom:6px}
.auth-card p{color:var(--mu);font-size:13.5px;margin-bottom:20px}
.auth-inp{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--ln);background:var(--bg);color:var(--tx);font-size:14.5px;margin-bottom:12px;outline:0}
.auth-inp:focus{border-color:var(--ac)}
.auth-btn{width:100%;padding:12px;border-radius:10px;background:var(--ac);color:#fff;font-weight:600;font-size:15px;margin-top:6px}
#auth-err{color:var(--danger);font-size:13px;margin-top:10px;display:none}
@media(max-width:800px){
 aside{position:fixed;inset:0 auto 0 0;z-index:5;transform:translateX(-100%);transition:transform .2s;box-shadow:0 0 30px rgba(0,0,0,.3)}
 body.open aside{transform:none}#menu{display:block}}
</style></head><body>

<div id="auth-view">
 <div class="auth-card">
  <h1>Drop</h1>
  <p>Sign in to access your vault</p>
  <form id="auth-form">
   <input class="auth-inp" id="l-user" type="text" placeholder="Username" autocomplete="username" required>
   <input class="auth-inp" id="l-pwd" type="password" placeholder="Password" autocomplete="current-password" required>
   <button class="auth-btn" type="submit">Sign In</button>
   <div id="auth-err"></div>
  </form>
 </div>
</div>

<aside>
 <h1><span>Drop</span><span class="badge" id="storage-badge">local</span></h1>
 <div id="fl"></div>
 <button class="add" id="nf">+ New folder</button>
</aside>
<main>
 <header>
  <button id="menu"><svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
  <h2 id="title"></h2>
  <button id="df">Delete folder</button>
  <button id="logout" title="Sign out"><svg style="width:19px;height:19px" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg></button>
 </header>
 <div id="feed"></div>
 <div id="bar">
  <input type="file" id="pick" multiple hidden>
  <button class="rb" id="at" title="Attach"><svg viewBox="0 0 24 24"><path d="M21 12l-8.5 8.5a5 5 0 01-7-7L14 5a3.3 3.3 0 014.7 4.7L10 18.4a1.7 1.7 0 01-2.4-2.4L15 8.6"/></svg></button>
  <textarea id="in" rows="1" placeholder="Type a note or drop files"></textarea>
  <button class="rb" id="sd" title="Send"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
 </div>
</main>
<div id="toast"></div>
<dialog id="dlg"><h3>Move to folder</h3><div id="dl"></div></dialog>
<script>
const $=s=>document.querySelector(s);
let cur=localStorage.f||"General",last="";
const q=encodeURIComponent;
const api=async(u,o={})=>{
 const r=await fetch(u,o);
 if(r.status===401){showAuth(true);return null}
 return r.json().catch(()=>({}));
};
const post=(u,b)=>api(u,{method:"POST",body:typeof b=="string"?b:JSON.stringify(b)});
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const sz=n=>n>1e6?(n/1e6).toFixed(1)+" MB":Math.ceil(n/1e3)+" KB";
const tm=t=>new Date(t*1000).toLocaleString([],{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});
const url=n=>`/files/${q(cur)}/${q(n)}`;
const toast=t=>{$("#toast").textContent=t;$("#toast").style.display=t?"block":"none"};
let fs=[];

function showAuth(v){
 $("#auth-view").style.display=v?"flex":"none";
 if(v){$("#l-user").focus()}
}

$("#auth-form").onsubmit=async e=>{
 e.preventDefault();
 const u=$("#l-user").value.trim(),p=$("#l-pwd").value;
 const res=await fetch("/api/login",{method:"POST",body:JSON.stringify({username:u,password:p})});
 const j=await res.json().catch(()=>({}));
 if(res.ok&&j.ok){
  $("#auth-err").style.display="none";
  showAuth(false);
  updateBadge(j.storage);
  refresh(true);
 }else{
  $("#auth-err").textContent=j.error||"Login failed";
  $("#auth-err").style.display="block";
 }
};

$("#logout").onclick=async()=>{
 await fetch("/api/logout",{method:"POST"});
 showAuth(true);
};

function updateBadge(mode){
 const b=$("#storage-badge");
 if(mode==="gdrive"){b.textContent="☁️ Drive";b.title="Google Drive Storage"}
 else{b.textContent="💾 local";b.title="Local Vault Storage"}
}

async function folders(){
 fs=await api("/api/folders");
 if(!fs)return;
 if(!fs.find(f=>f.name==cur))cur="General";
 $("#fl").innerHTML=fs.map(f=>`<button class="f ${f.name==cur?"on":""}" data-f="${esc(f.name)}"><span>${esc(f.name)}</span><small>${f.count}</small></button>`).join("");
 $("#title").textContent=cur;$("#df").style.display=cur=="General"?"none":"";
}

async function items(force){
 const it=await api("/api/items?f="+q(cur));
 if(!it)return;
 const s=JSON.stringify(it);
 if(s==last&&!force)return;last=s;
 const fd=$("#feed"),near=fd.scrollHeight-fd.scrollTop-fd.clientHeight<140;
 fd.innerHTML=it.length?it.map(x=>{
  const n=x.name,ext=n.split(".").pop().toLowerCase();let b;
  if(x.text!=null)b=`<div class="t">${esc(x.text)}</div>`;
  else if(/^(jpe?g|png|gif|webp|bmp|avif)$/.test(ext))b=`<a href="${url(n)}" target="_blank"><img loading="lazy" src="${url(n)}"></a>`;
  else b=`<a class="file" href="${url(n)}" target="_blank"><span class="ic">${esc(ext.slice(0,4).toUpperCase())}</span><span><b>${esc(n)}</b><small>${sz(x.size)}</small></span></a>`;
  return `<div class="m">${b}<div class="meta">${tm(x.time)} · <a data-a="mv" data-n="${esc(n)}">Move</a> · <a data-a="rm" data-n="${esc(n)}">Delete</a></div></div>`}).join("")
  :`<p class="empty">Nothing here yet. Drop files or type a note below.</p>`;
 if(near||force)fd.scrollTop=fd.scrollHeight;
}

const refresh=async force=>{await folders();await items(force)};
const go=f=>{cur=f;localStorage.f=f;last="";document.body.classList.remove("open");refresh(true)};

async function up(files){
 for(const f of files){
  toast("Uploading "+f.name+"…");
  const r=await fetch(`/api/upload?f=${q(cur)}&name=${q(f.name)}`,{method:"POST",body:f});
  if(r.status===401){showAuth(true);return}
 }
 toast("");refresh(true);
}
async function send(){
 const t=$("#in").value.trim();if(!t)return;
 $("#in").value="";$("#in").style.height="";
 await post("/api/note?f="+q(cur),t);refresh(true);
}

$("#fl").onclick=e=>{const b=e.target.closest("[data-f]");if(b)go(b.dataset.f)};
$("#nf").onclick=async()=>{const n=prompt("Folder name");if(n&&n.trim()){await post("/api/folder",n.trim());go(n.trim())}};
$("#df").onclick=async()=>{if(confirm(`Delete "${cur}" and everything in it?`)){await post("/api/delete",{f:cur});go("General")}};
$("#menu").onclick=()=>document.body.classList.toggle("open");
$("#at").onclick=()=>$("#pick").click();
$("#pick").onchange=e=>{up([...e.target.files]);e.target.value=""};
$("#sd").onclick=send;
$("#in").onkeydown=e=>{if(e.key=="Enter"&&!e.shiftKey&&innerWidth>800){e.preventDefault();send()}};
$("#in").oninput=e=>{e.target.style.height="auto";e.target.style.height=e.target.scrollHeight+"px"};
$("#feed").onclick=async e=>{
 const a=e.target.closest("[data-a]");if(!a)return;const n=a.dataset.n;
 if(a.dataset.a=="rm"){if(confirm("Delete this?")){await post("/api/delete",{f:cur,name:n});refresh(true)}}
 else{$("#dl").innerHTML=fs.filter(f=>f.name!=cur).map(f=>`<button class="f" data-t="${esc(f.name)}">${esc(f.name)}</button>`).join("")||"<p class='empty'>Create another folder first.</p>";
  $("#dlg").dataset.n=n;$("#dlg").showModal()}
};
$("#dlg").onclick=async e=>{
 const b=e.target.closest("[data-t]");
 if(b){await post("/api/move",{f:cur,name:$("#dlg").dataset.n,to:b.dataset.t});$("#dlg").close();refresh(true)}
 else if(e.target==$("#dlg"))$("#dlg").close();
};
document.ondragover=e=>{e.preventDefault();document.body.classList.add("drag")};
document.ondragleave=e=>{if(!e.relatedTarget)document.body.classList.remove("drag")};
document.ondrop=e=>{e.preventDefault();document.body.classList.remove("drag");up([...e.dataTransfer.files])};
document.onpaste=e=>{const f=[...e.clipboardData.files];if(f.length)up(f)};

async function init(){
 const st=await fetch("/api/status").then(r=>r.json()).catch(()=>({}));
 updateBadge(st.storage);
 if(!st.authenticated){showAuth(true)}
 else{refresh(true)}
}
init();
setInterval(()=>{if($("#auth-view").style.display!=="flex")refresh(false)},3000);
</script></body></html>"""

if __name__ == "__main__":
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "localhost"
    print(f"Drop server running at: http://{ip}:{PORT} (Local: http://localhost:{PORT})")
    print(f"Default Login: Username = '{DROP_USER}' | Password = '{DROP_PASS}'")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
