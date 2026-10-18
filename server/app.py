from __future__ import annotations

import copy,json,math,os,re,shutil,socket,ipaddress,threading,time,uuid,zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal,Optional
from fastapi import FastAPI,HTTPException,UploadFile,File,Request,Form
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,ConfigDict,model_validator
from . import media,ai,ghl

ROOT=Path(__file__).resolve().parent.parent
DATA=Path(os.environ.get('CLIPFORGE_DATA',ROOT/'data')).resolve(); DATA.mkdir(parents=True,exist_ok=True)
LOCK=threading.RLock(); POOL=ThreadPoolExecutor(max_workers=1); JOBS={}; EVENTS={}
app=FastAPI(title='ClipForge Local',docs_url=None,redoc_url=None)
EXTENSION_ORIGIN='chrome-extension://hllhhgmemaeimjjecboegbfgbkbehokk'
ALLOWED_ORIGINS={'http://127.0.0.1:43177','http://localhost:43177',EXTENSION_ORIGIN}

BRAND=dict(font='Arial',color='#FFFFFF',highlight='#C8ED48',style='pop')
LINKS=dict(site='',course='')
PROFILE={}
FONTS=('Arial','Helvetica','Impact','Georgia','Avenir Next','Menlo')
def ai_settings_path(): return DATA/'settings.json'
def brand_public(): return dict(font=BRAND['font'],color=BRAND['color'],highlight=BRAND['highlight'],style=BRAND['style'])
def links_public(): return dict(site=LINKS['site'],course=LINKS['course'])
def url_ok(value,fallback):
    text=str(value or '').strip()
    if not text: return ''
    return text if re.fullmatch(r'https?://[^\s]{3,180}',text) else fallback
def apply_links(body):
    if body.get('site') is not None: LINKS['site']=url_ok(body['site'],LINKS['site'])
    if body.get('course') is not None: LINKS['course']=url_ok(body['course'],LINKS['course'])
def hex_ok(value,fallback):
    text=str(value or '').strip()
    return text.upper() if re.fullmatch(r'#[0-9A-Fa-f]{6}',text) else fallback
def apply_brand(body):
    if body.get('font'):
        font=str(body['font']).strip()
        BRAND['font']=font if font in FONTS else 'Arial'
    if body.get('color') is not None: BRAND['color']=hex_ok(body['color'],BRAND['color'])
    if body.get('highlight') is not None: BRAND['highlight']=hex_ok(body['highlight'],BRAND['highlight'])
    if body.get('style') in {'pop','clean'}: BRAND['style']=body['style']
def clip_brand(extra=None):
    extra=extra or {}
    return dict(caption_style=extra.get('caption_style') or BRAND['style'],caption_font=extra.get('caption_font') or BRAND['font'],caption_color=hex_ok(extra.get('caption_color'),BRAND['color']),caption_highlight=hex_ok(extra.get('caption_highlight'),BRAND['highlight']))
def load_ai():
    raw={}
    path=ai_settings_path()
    if path.exists():
        try: raw=json.loads(path.read_text(encoding='utf-8'))
        except Exception: raw={}
    if raw.get('key'): ai.KEY=str(raw['key'])
    elif os.environ.get('OPENROUTER_API_KEY') and not ai.KEY: ai.KEY=os.environ.get('OPENROUTER_API_KEY','')
    if raw.get('model'): ai.MODEL=str(raw['model'])
    if raw.get('stt_model'): ai.STT=str(raw['stt_model'])
    apply_brand(raw.get('brand') or raw); apply_links(raw.get('links') or raw)
    PROFILE.update(raw.get('profile') or {})
def save_ai():
    path=ai_settings_path(); temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(key=ai.KEY,model=ai.MODEL,stt_model=ai.STT,brand=brand_public(),links=links_public(),profile=dict(PROFILE)),indent=2), encoding='utf-8'); temp.replace(path)
load_ai()

@app.middleware('http')
async def local_only(request,call_next):
    host=request.headers.get('host','')
    if host not in {'127.0.0.1:43177','localhost:43177','testserver'}: return JSONResponse({'detail':'Invalid host'},403)
    origin=request.headers.get('origin')
    if origin and origin not in ALLOWED_ORIGINS: return JSONResponse({'detail':'Cross-origin access denied'},403)
    if request.method=='OPTIONS':
        response=Response(status_code=204)
    else:
        if request.method not in {'GET','HEAD'} and request.headers.get('x-clipforge')!='1': return JSONResponse({'detail':'Missing request header'},403)
        response=await call_next(request)
    if origin:
        response.headers['Access-Control-Allow-Origin']=origin
        response.headers['Vary']='Origin'
        response.headers['Access-Control-Allow-Headers']='Content-Type, X-ClipForge'
        response.headers['Access-Control-Allow-Methods']='GET, HEAD, POST, PUT, DELETE, OPTIONS'
    response.headers['Cross-Origin-Resource-Policy']='cross-origin'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'"
    return response

def uid(): return uuid.uuid4().hex[:16]
def folder(pid):
    if not re.fullmatch('[a-f0-9]{16}',pid): raise HTTPException(404,'Project not found')
    p=DATA/pid
    if not (p/'project.json').exists(): raise HTTPException(404,'Project not found')
    return p

def read(pid):
    with LOCK: return json.loads((folder(pid)/'project.json').read_text(encoding='utf-8'))
def write(p):
    with LOCK:
        path=DATA/p['id']/'project.json'; temp=path.with_suffix('.tmp'); temp.write_text(json.dumps(p,ensure_ascii=False,indent=2), encoding='utf-8'); temp.replace(path)
def create(title,folder_id=None):
    if folder_id: get_collection(folder_id)
    pid=uid(); (DATA/pid).mkdir(); p=dict(id=pid,title=title,created=time.time(),state='importing',clips=[],transcript=None,source=None,folder_id=folder_id)
    write(p); return p

def queue(pid,kind,fn):
    with LOCK:
        if any(j['project']==pid and j['state'] in ('queued','running') for j in JOBS.values()): raise HTTPException(409,'This project is already processing. Wait or cancel first.')
        jid=uid(); JOBS[jid]=dict(id=jid,project=pid,kind=kind,state='queued',message='Queued',progress=0); EVENTS[jid]=threading.Event()
    def work():
        media.CTX.cancel=EVENTS[jid]
        def progress(message,fraction):
            media.check()
            with LOCK: JOBS[jid].update(message=message,progress=round(fraction,3))
        try:
            with LOCK: JOBS[jid]['state']='running'
            media.check(); fn(progress)
            with LOCK: JOBS[jid].update(state='done',message='Complete',progress=1)
        except Exception as e:
            with LOCK: JOBS[jid].update(state='cancelled' if isinstance(e,media.Cancelled) else 'error',message=str(e)[-2000:])
        finally:
            if kind=='import' and JOBS[jid]['state'] in ('error','cancelled'):
                failed=read(pid);failed['state']='error';failed['error']=JOBS[jid]['message'];write(failed)
            media.CTX.cancel=None
    POOL.submit(work); return JOBS[jid].copy()

def require_ready(pid):
    p=read(pid)
    if p['state']!='ready': raise HTTPException(409,'Wait for video import to finish.')
    return p

def ensure_idle(pid):
    if any(j['project']==pid and j['state'] in ('queued','running') for j in JOBS.values()): raise HTTPException(409,'Wait for this project to finish processing before editing.')

def validate_url(url):
    parsed=urlparse(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password: raise ValueError('Enter a public HTTPS video or YouTube URL.')
    addresses=socket.getaddrinfo(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses): raise ValueError('Local and private-network URLs are not supported. Upload the file instead.')
    return url

class ImportRequest(BaseModel):
    url:str=Field(max_length=4096)
    folder_id:Optional[str]=None
class CollectionEdit(BaseModel):
    name:str=Field(min_length=1,max_length=120)
    instructions:str=Field(default='',max_length=6000)
    channel_url:str=Field(default='',max_length=500)
class VideoEdit(BaseModel):
    title:Optional[str]=Field(default=None,min_length=1,max_length=200)
    folder_id:Optional[str]=None

class CreatorProfile(BaseModel):
    name:str=Field(default='',max_length=120)
    about:str=Field(default='',max_length=2000)
    audience:str=Field(default='',max_length=500)
    tone:str=Field(default='',max_length=300)
    offer:str=Field(default='',max_length=1000)
    cta:str=Field(default='',max_length=160)
    avoid:str=Field(default='',max_length=1000)

class Settings(BaseModel):
    profile:Optional[CreatorProfile]=None
    key:Optional[str]=Field(default=None,max_length=512)
    model:str=Field(default='google/gemini-2.5-flash',max_length=120)
    stt_model:str=Field(default='openai/whisper-1',max_length=120)
    ghl_token:Optional[str]=Field(default=None,max_length=4096)
    ghl_location:Optional[str]=Field(default=None,max_length=80)
    ghl_user:Optional[str]=Field(default=None,max_length=80)
    ghl_accounts:Optional[list[str]]=None
    caption_font:Optional[str]=Field(default=None,max_length=40)
    caption_color:Optional[str]=Field(default=None,max_length=7)
    caption_highlight:Optional[str]=Field(default=None,max_length=7)
    caption_style:Optional[Literal['pop','clean']]=None
    site_url:Optional[str]=Field(default=None,max_length=200)
    course_url:Optional[str]=Field(default=None,max_length=200)
class GhlPost(BaseModel):
    account_ids:list[str]=Field(min_length=1,max_length=20)
    summary:str=Field(default='',max_length=5000)
    follow_up:str=Field(default='',max_length=1200)
    schedule_date:str=Field(default='',max_length=40)
    type:Literal['post','story','reel']='reel'
    status:Literal['draft','scheduled','published']='scheduled'
    user_id:str=Field(default='',max_length=80)
class GhlMove(BaseModel):
    schedule_date:str=Field(max_length=40)
class QuickCopy(BaseModel):
    clip_id:str=Field(max_length=80)
    summary:str=Field(min_length=1,max_length=5000)
    follow_up:str=Field(default='',max_length=1200)

class GhlQuick(BaseModel):
    posts:Optional[list[QuickCopy]]=Field(default=None,max_length=20)
    clip_ids:list[str]=Field(min_length=1,max_length=20)
    account_ids:list[str]=Field(min_length=1,max_length=20)
    follow_up:str=Field(default='',max_length=1200)
    start:str=Field(max_length=40)
    interval_hours:float=Field(default=6,ge=.25,le=168)
    type:Literal['post','story','reel']='reel'
    user_id:str=Field(default='',max_length=80)
    @model_validator(mode='after')
    def validate_posts(self):
        if len(set(self.clip_ids)) != len(self.clip_ids):
            raise ValueError('Choose each clip only once.')
        if self.posts is not None:
            ids=[post.clip_id for post in self.posts]
            if len(ids)!=len(set(ids)) or set(ids)!=set(self.clip_ids):
                raise ValueError('Review one post for every selected clip.')
        return self
class Analyze(BaseModel):
    transcription:Literal['local','openrouter']='local'
    selection:Literal['local','openrouter','ollama']='local'
    local_model:Literal['tiny.en','small','medium']='small'
    count:int=Field(default=4,ge=1,le=12)
    length:int=Field(default=45,ge=10,le=180)
    brief:str=Field(default='',max_length=2000)
    auto_export:bool=True
    window_start:Optional[float]=Field(default=None,ge=0,allow_inf_nan=False)
    window_end:Optional[float]=Field(default=None,gt=0,allow_inf_nan=False)
    caption_style:Optional[Literal['pop','clean']]=None
    caption_font:Optional[str]=Field(default=None,max_length=40)
    caption_color:Optional[str]=Field(default=None,max_length=7)
    caption_highlight:Optional[str]=Field(default=None,max_length=7)
class Keyframe(BaseModel):
    time:float=Field(ge=0,le=600,allow_inf_nan=False)
    x:float=Field(default=.5,ge=0,le=1,allow_inf_nan=False)
    y:float=Field(default=.5,ge=0,le=1,allow_inf_nan=False)
    zoom:float=Field(default=1,ge=1,le=1.6,allow_inf_nan=False)
class Clip(BaseModel):
    model_config=ConfigDict(extra='ignore')
    title:str=Field(default='New clip',max_length=120)
    start:float=Field(ge=0,allow_inf_nan=False)
    end:float=Field(gt=0,allow_inf_nan=False)
    aspect:Literal['vertical','square','landscape']='vertical'
    framing:Literal['face','center','fit','split']='face'
    focus_x:float=Field(default=.5,ge=0,le=1,allow_inf_nan=False)
    captions:bool=True
    caption_style:Literal['pop','clean']='pop'
    caption_font:str=Field(default='Arial',max_length=40)
    caption_color:str=Field(default='#FFFFFF',max_length=7)
    caption_highlight:str=Field(default='#C8ED48',max_length=7)
    remove_silence:bool=False
    remove_fillers:bool=True
    auto_zoom:bool=True
    resolution:Literal[720,1080]=1080
    keyframes:list[Keyframe]=Field(default_factory=list,max_length=80)
    @model_validator(mode='after')
    def validate_bounds(self):
        if not 1<=self.end-self.start<=600: raise ValueError('A clip must be 1–600 seconds long.')
        if any(k.time>self.end-self.start for k in self.keyframes): raise ValueError('Keyframes must fall inside the clip.')
        if len({k.time for k in self.keyframes})!=len(self.keyframes): raise ValueError('Keyframe times must be unique.')
        if not re.fullmatch(r'#[0-9A-Fa-f]{6}',self.caption_color): raise ValueError('Caption color must be #RRGGBB.')
        if not re.fullmatch(r'#[0-9A-Fa-f]{6}',self.caption_highlight): raise ValueError('Highlight color must be #RRGGBB.')
        self.caption_color=self.caption_color.upper(); self.caption_highlight=self.caption_highlight.upper()
        self.caption_font=self.caption_font if self.caption_font in FONTS else 'Arial'
        return self
class Word(BaseModel):
    start:float=Field(ge=0,allow_inf_nan=False); end:float=Field(gt=0,allow_inf_nan=False); word:str=Field(max_length=200)
class TranscriptEdit(BaseModel): words:list[Word]=Field(max_length=100000)

def finish_import(p,path,progress):
    progress('Checking video and preparing browser playback',.7)
    meta=media.probe(path)
    final=DATA/p['id']/'source.mp4'
    media.normalize(path,final)
    media.ff('-ss',str(min(2,meta['duration']/2)),'-i',final,'-frames:v','1','-vf','scale=640:-2','-update','1',DATA/p['id']/'poster.jpg')
    p.update(meta=media.probe(final),state='ready'); write(p)
    if path!=final: path.unlink(missing_ok=True)

@app.get('/api/health')
def health(): return dict(ok=True,key_set=bool(ai.KEY),model=ai.MODEL,stt_model=ai.STT,ffmpeg=Path(media.FFMPEG).name,ghl=ghl.public(ghl.load(DATA)),brand=brand_public(),links=links_public(),profile=dict(PROFILE))
@app.post('/api/settings')
def settings(body:Settings):
    if body.key is not None: ai.KEY=body.key.strip()
    ai.MODEL=body.model; ai.STT=body.stt_model
    apply_brand(dict(font=body.caption_font,color=body.caption_color,highlight=body.caption_highlight,style=body.caption_style))
    apply_links(dict(site=body.site_url,course=body.course_url))
    if body.profile is not None:
        PROFILE.clear(); PROFILE.update(body.profile.model_dump())
    save_ai()
    ghl_body={}
    if body.ghl_token is not None: ghl_body['token']=body.ghl_token
    if body.ghl_location is not None: ghl_body['location']=body.ghl_location
    if body.ghl_user is not None: ghl_body['user']=body.ghl_user
    if body.ghl_accounts is not None: ghl_body['accounts']=body.ghl_accounts
    saved=ghl.save(DATA,ghl_body) if ghl_body else ghl.public(ghl.load(DATA))
    return dict(key_set=bool(ai.KEY),ghl=saved,brand=brand_public(),links=links_public(),profile=dict(PROFILE))
@app.get('/api/ghl/accounts')
def ghl_accounts(): return ghl.accounts(ghl.require(DATA))
@app.get('/api/ghl/users')
def ghl_users(): return dict(users=ghl.users(ghl.require(DATA)))
@app.get('/api/ghl/posts')
def ghl_posts(from_date:str,to_date:str):
    return ghl.list_posts(ghl.require(DATA),from_date,to_date)
@app.get('/api/ghl/posts/{post_id}')
def ghl_post(post_id:str):
    return ghl.get_post(ghl.require(DATA),post_id)
@app.put('/api/ghl/posts/{post_id}')
def ghl_move(post_id:str,body:GhlMove):
    cfg=ghl.require(DATA); current=ghl.get_post(cfg,post_id)
    payload={k:current[k] for k in current if k in {'accountIds','summary','media','type','followUpComment','userId','tiktokPostDetails','instagramPostDetails','facebookPostDetails','youtubePostDetails','gmbPostDetails','linkedinPostDetails'}}
    if not payload.get('accountIds') and current.get('accountIds'): payload['accountIds']=current['accountIds']
    payload.update(status='scheduled',scheduleDate=ghl.future_iso(body.schedule_date),scheduleTimeUpdated=True)
    if cfg.get('user'): payload['userId']=cfg['user']
    return ghl.edit_post(cfg,post_id,payload)
@app.delete('/api/ghl/posts/{post_id}')
def ghl_delete(post_id:str):
    return ghl.delete_post(ghl.require(DATA),post_id)
def collections():
    path=DATA/'folders.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
def save_collections(items):
    path=DATA/'folders.json';temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(items,ensure_ascii=False,indent=2), encoding='utf-8');temp.replace(path)
def get_collection(fid):
    item=next((f for f in collections() if f['id']==fid),None)
    if item is None: raise HTTPException(404,'Folder not found')
    return item
def project_profile(p):
    profile=dict(PROFILE)
    if p.get('folder_id'):
        f=get_collection(p['folder_id'])
        profile['folder_instructions']=f.get('instructions','')
    return profile
@app.get('/api/folders')
def list_collections():
    with LOCK: return collections()
@app.post('/api/folders')
def new_collection(body:CollectionEdit):
    with LOCK:
        items=collections();item=dict(id=uid(),**body.model_dump());items.append(item);save_collections(items);return item
@app.put('/api/folders/{fid}')
def edit_collection(fid:str,body:CollectionEdit):
    with LOCK:
        get_collection(fid);items=collections();item=dict(id=fid,**body.model_dump());save_collections([item if f['id']==fid else f for f in items]);return item
@app.delete('/api/folders/{fid}')
def delete_collection(fid:str):
    with LOCK:
        get_collection(fid)
        members=[p for p in projects() if p.get('folder_id')==fid]
        for p in members: ensure_idle(p['id'])
        for p in members:
            full=read(p['id']);full['folder_id']=None;write(full)
        save_collections([f for f in collections() if f['id']!=fid])
    return dict(ok=True)
@app.put('/api/projects/{pid}')
def update_video(pid:str,body:VideoEdit):
    with LOCK:
        ensure_idle(pid);p=read(pid)
        if body.title is not None: p['title']=body.title
        if 'folder_id' in body.model_fields_set:
            if body.folder_id: get_collection(body.folder_id)
            p['folder_id']=body.folder_id
        write(p);return p
@app.delete('/api/projects/{pid}')
def delete_video(pid:str):
    with LOCK:
        ensure_idle(pid);source=folder(pid);trash=DATA/'.trash';trash.mkdir(exist_ok=True)
        source.rename(trash/pid)
    return dict(ok=True,restore_id=pid)
@app.post('/api/trash/{pid}/restore')
def restore_video(pid:str):
    if not re.fullmatch('[a-f0-9]{16}',pid): raise HTTPException(404)
    with LOCK:
        source=DATA/'.trash'/pid
        if not (source/'project.json').exists(): raise HTTPException(404,'Deleted video not found')
        if (DATA/pid).exists(): raise HTTPException(409,'Video already exists')
        source.rename(DATA/pid);p=read(pid)
        if p.get('folder_id') and not any(f['id']==p['folder_id'] for f in collections()):p['folder_id']=None;write(p)
    return p
@app.get('/api/folders/{fid}/channel-videos')
def channel_videos(fid:str):
    import sys
    f=get_collection(fid);url=f.get('channel_url','');parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname not in {'youtube.com','www.youtube.com'} or not re.match(r'^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/videos)?/?$',parsed.path):
        raise HTTPException(400,'Use a YouTube channel URL, such as https://www.youtube.com/@yourchannel')
    url='https://www.youtube.com'+parsed.path.rstrip('/').removesuffix('/videos')+'/videos'
    output=media.run([sys.executable,'-m','yt_dlp','--ignore-config','--flat-playlist','--playlist-end','30','--dump-single-json','--skip-download',url])
    info=json.loads(output);return [dict(id=e.get('id'),title=e.get('title') or 'Video',url='https://www.youtube.com/watch?v='+e['id']) for e in info.get('entries',[]) if e.get('id') and re.fullmatch(r'[A-Za-z0-9_-]{11}',e['id'])]

@app.get('/api/projects')
def projects():
    out=[]
    for f in DATA.glob('*/project.json'):
        p=json.loads(f.read_text(encoding='utf-8')); p.pop('transcript',None); out.append(p)
    return sorted(out,key=lambda p:p['created'],reverse=True)
@app.get('/api/projects/{pid}')
def project(pid:str): return read(pid)
@app.get('/api/jobs')
def jobs():
    with LOCK: return list(JOBS.values())[-100:]
@app.post('/api/jobs/{jid}/cancel')
def cancel(jid:str):
    if jid not in EVENTS: raise HTTPException(404)
    EVENTS[jid].set(); return {'ok':True}

@app.post('/api/import/url')
def import_url(body:ImportRequest):
    try: validate_url(body.url)
    except Exception as e: raise HTTPException(400,str(e))
    p=create('Importing video…',body.folder_id); p['source']=body.url; write(p)
    def work(progress):
        import sys
        dest=DATA/p['id']; progress('Downloading source video',.05)
        # Isolated config; never harvest browser cookies or credentials.
        output=media.run([sys.executable,'-m','yt_dlp','--ignore-config','--no-playlist','--no-cache-dir',
                         '--ffmpeg-location',media.FFMPEG,'--max-filesize','8G',
                         '--match-filter','duration <= 14400 & !is_live',
                         '-f','bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/b[vcodec^=avc1][height<=1080]/bv*[height<=1080]+ba/b[height<=1080]/best','--merge-output-format','mp4',
                         '--write-info-json','--no-warnings','-o',str(dest/'download.%(ext)s'),body.url])
        info=json.loads((dest/'download.info.json').read_text(encoding='utf-8')); p['title']=str(info.get('title','Imported video'))[:200]; write(p)
        paths=[x for x in dest.glob('download.*') if x.suffix.lower() in ('.mp4','.mkv','.webm','.mov','.avi')]
        if not paths: raise ValueError('No video was downloaded. Upload the original file if the site blocks downloading.')
        finish_import(p,paths[0],progress)
    return dict(project=p,job=queue(p['id'],'import',work))

@app.post('/api/import/upload')
async def upload(file:UploadFile=File(...),folder_id:Optional[str]=Form(default=None)):
    p=create((file.filename or 'Uploaded video')[:200],folder_id); target=DATA/p['id']/'upload.bin'; size=0
    try:
        with target.open('wb') as f:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>8*1024**3: raise HTTPException(413,'Maximum file size is 8 GB.')
                f.write(chunk)
    except Exception:
        target.unlink(missing_ok=True); raise
    finally: await file.close()
    return dict(project=p,job=queue(p['id'],'import',lambda progress:finish_import(p,target,progress)))

def export_one(p,c,progress):
    cid=c['id']; exportid=uid(); location=DATA/p['id']/'exports'/exportid
    result=media.render(DATA/p['id']/'source.mp4',p['meta'],c,(p.get('transcript') or {}).get('words',[]),location,progress)
    c['export']=dict(id=exportid,**result); c['dirty']=False; write(p)

@app.post('/api/projects/{pid}/analyze')
def analyze(pid:str,body:Analyze):
    p=require_ready(pid)
    if (body.transcription=='openrouter' or body.selection=='openrouter') and not ai.KEY: raise HTTPException(400,'Enter your OpenRouter key in Settings first.')
    if not p['meta']['has_audio']: raise HTTPException(400,'This video has no audio. Add a manual clip instead.')
    if (body.window_start is None) ^ (body.window_end is None): raise HTTPException(400,'Set both ends of the timeline selection.')
    if body.window_start is not None and not 1<=body.window_end-body.window_start<=p['meta']['duration']+1: raise HTTPException(400,'That timeline selection is too short.')
    apply_brand(dict(font=body.caption_font,color=body.caption_color,highlight=body.caption_highlight,style=body.caption_style)); save_ai()
    style=clip_brand(dict(caption_style=body.caption_style,caption_font=body.caption_font,caption_color=body.caption_color,caption_highlight=body.caption_highlight))
    window=(body.window_start,body.window_end) if body.window_start is not None else None
    def work(progress):
        if not p.get('transcript'):
            p['transcript']=ai.transcribe(DATA/pid/'source.mp4',DATA/pid,p['meta']['duration'],body.transcription,progress,body.local_model); write(p)
        instructions=project_profile(p).get('folder_instructions','')
        brief='\n\n'.join(x for x in [instructions,body.brief] if x)
        candidates=ai.suggest(p['transcript'],p['meta']['duration'],body.count,body.length,body.selection,brief,progress,window,folder=DATA/pid)
        fresh=[]
        for x in candidates:
            c=Clip(**{**x,**style}).model_dump(); c.update(id=uid(),score=x['score'],reason=x['reason'],selection=body.selection,dirty=True)
            fresh.append(c)
        p['clips'].extend(fresh); write(p)
        if body.auto_export:
            for i,c in enumerate(fresh):
                export_one(p,c,lambda m,f:progress(f'Clip {i+1}/{len(fresh)} · {m}',f))
        for i,c in enumerate(fresh):
            progress(f'Writing social posts · {i+1}/{len(fresh)}',.92)
            c['social']=ai.social_copy(c.get('title'),ghl.caption(p,c),LINKS,project_profile(p)); write(p)
    return queue(pid,'analyze',work)

@app.post('/api/projects/{pid}/clips')
def add_clip(pid:str,body:Clip):
    p=require_ready(pid); ensure_idle(pid)
    if body.end>p['meta']['duration']+.05: raise HTTPException(400,'Clip ends after the source video.')
    c=Clip(**{**clip_brand(),**body.model_dump()}).model_dump(); c.update(id=uid(),dirty=True,selection='manual',reason='Manual selection',score=None)
    p['clips'].append(c); write(p); return c
@app.put('/api/projects/{pid}/clips/{cid}')
def edit_clip(pid:str,cid:str,body:Clip):
    p=require_ready(pid); ensure_idle(pid)
    if body.end>p['meta']['duration']+.05: raise HTTPException(400,'Clip ends after the source video.')
    c=next((c for c in p['clips'] if c['id']==cid),None)
    if c is None: raise HTTPException(404)
    c.update(body.model_dump(),dirty=True); write(p); return c
@app.put('/api/projects/{pid}/transcript')
def edit_transcript(pid:str,body:TranscriptEdit):
    p=require_ready(pid); ensure_idle(pid); words=[w.model_dump() for w in body.words]
    if any(w['start']>=w['end'] or w['end']>p['meta']['duration']+.1 for w in words): raise HTTPException(400,'Invalid word timestamps.')
    words.sort(key=lambda w:w['start']); p['transcript']={**(p.get('transcript') or {}),'words':words,'segments':media.segments_from_words(words)}
    for c in p['clips']: c['dirty']=True
    write(p); return {'ok':True}
@app.post('/api/projects/{pid}/export')
def export_all(pid:str):
    p=require_ready(pid)
    if not p['clips']: raise HTTPException(400,'Add a clip first.')
    def work(progress):
        pending=[c for c in p['clips'] if c.get('dirty',True) or not c.get('export')]
        for i,c in enumerate(pending): export_one(p,c,lambda m,f:progress(f'Clip {i+1}/{len(pending)} · {m}',f))
    return queue(pid,'export',work)
@app.post('/api/projects/{pid}/clips/{cid}/export')
def export_clip(pid:str,cid:str):
    p=require_ready(pid); c=next((c for c in p['clips'] if c['id']==cid),None)
    if c is None: raise HTTPException(404)
    return queue(pid,'export',lambda progress:export_one(p,c,progress))
@app.get('/api/saved')
def saved_clips():
    items=[]
    for path in DATA.glob('*/project.json'):
        try: p=json.loads(path.read_text(encoding='utf-8'))
        except Exception: continue
        for c in p.get('clips') or []:
            if not c.get('saved'): continue
            export=c.get('export') or {}
            items.append(dict(project_id=p['id'],project=p.get('title') or 'Project',clip_id=c['id'],title=c.get('title') or 'Clip',start=c.get('start'),end=c.get('end'),saved_at=c.get('saved_at') or p.get('created') or 0,ghl=bool((c.get('ghl') or {}).get('url')),export_id=export.get('id')))
    items.sort(key=lambda x:x['saved_at'],reverse=True)
    return dict(clips=items[:80])
def save_clip_media(p,c,progress):
    c['saved']=True; c['saved_at']=time.time(); write(p)
    cfg=ghl.load(DATA)
    if not cfg.get('token') or not cfg.get('location'): return
    stored,_=ghl.ensure_media(cfg,DATA/p['id'],c,progress)
    c['ghl']={**(c.get('ghl') or {}),**stored}; write(p)
@app.post('/api/projects/{pid}/clips/{cid}/save')
def save_one(pid:str,cid:str):
    p=require_ready(pid); c=next((c for c in p['clips'] if c['id']==cid),None)
    if c is None: raise HTTPException(404)
    if not c.get('export') or c.get('dirty'): raise HTTPException(400,'Render this clip first, then save it.')
    return queue(pid,'save',lambda progress:save_clip_media(p,c,progress))
def clip_social(p,c):
    social=c.get('social')
    if not social or not social.get('posts') or social.get('_context') != ai.social_context(LINKS,project_profile(p)):
        social=ai.social_copy(c.get('title'),ghl.caption(p,c),LINKS,project_profile(p)); c['social']=social; write(p)
    return social
@app.post('/api/projects/{pid}/clips/{cid}/copy')
def write_copy(pid:str,cid:str):
    p=require_ready(pid); c=next((x for x in p['clips'] if x['id']==cid),None)
    if c is None: raise HTTPException(404)
    c['social']=ai.social_copy(c.get('title'),ghl.caption(p,c),LINKS,project_profile(p)); write(p)
    return c['social']
@app.get('/api/projects/{pid}/bundle')
def bundle(pid:str):
    p=read(pid); ensure_idle(pid); path=folder(pid)/'clips.zip'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_STORED) as z:
        for i,c in enumerate(p['clips']):
            if c.get('export') and not c.get('dirty'):
                base=folder(pid)/'exports'/c['export']['id']; name=re.sub(r'[^\w -]','',c['title'])[:65] or 'clip'
                z.write(base/'clip.mp4',f'{i+1:02}-{name}.mp4'); z.write(base/'captions.srt',f'{i+1:02}-{name}.srt')
        z.writestr('project.json',json.dumps(p,indent=2,ensure_ascii=False))
    return FileResponse(path,filename='ClipForge-clips.zip',media_type='application/zip')
@app.get('/media/{pid}/{name:path}')
def asset(pid:str,name:str,download:bool=False):
    base=folder(pid); path=(base/name).resolve()
    if not path.is_relative_to(base) or not path.is_file() or path.suffix not in ('.mp4','.jpg','.srt'): raise HTTPException(404)
    return FileResponse(path,filename=path.name if download else None)

def publish_clip(p,c,body:GhlPost,progress):
    cfg=ghl.require(DATA)
    stored,media_items=ghl.ensure_media(cfg,DATA/p['id'],c,progress)
    c['ghl']=stored; write(p)
    progress('Creating the social post',.75)
    connected=ghl.accounts(cfg).get('accounts') or []
    payload=ghl.post_payload(cfg,connected,body.account_ids,body.summary or ghl.caption(p,c),body.follow_up,body.schedule_date,body.type,c,media_items,body.status,body.user_id)
    result=ghl.create_post(cfg,payload)
    posts=list(c.get('ghl',{}).get('posts') or [])
    posts.append(dict(id=result.get('_id') or result.get('id'),status=body.status,scheduleDate=payload.get('scheduleDate'),summary=payload.get('summary')))
    c['ghl']={**stored,'posts':posts[-20:]}; write(p)
    ghl.save(DATA,{'accounts':body.account_ids,'user':payload['userId']})
    return result

@app.post('/api/projects/{pid}/clips/{cid}/publish')
def publish_one(pid:str,cid:str,body:GhlPost):
    p=require_ready(pid); c=next((c for c in p['clips'] if c['id']==cid),None)
    if c is None: raise HTTPException(404)
    return queue(pid,'publish',lambda progress:publish_clip(p,c,body,progress))

@app.post('/api/projects/{pid}/publish')
def publish_many(pid:str,body:GhlQuick):
    p=require_ready(pid)
    by_id={c['id']:c for c in p['clips']}
    if any(cid not in by_id for cid in body.clip_ids): raise HTTPException(400,'One of the selected clips is missing.')
    clips=[by_id[cid] for cid in body.clip_ids]
    overrides={post.clip_id:post for post in body.posts or []}
    items=[]
    for i,c in enumerate(clips):
        if not c.get('export') or c.get('dirty'): raise HTTPException(400,'Render every selected clip before scheduling.')
        if c['id'] in overrides:
            chosen=overrides[c['id']]; summary=chosen.summary; follow_up=chosen.follow_up
        else:
            social=clip_social(p,c); posts=social.get('posts') or []; comments=social.get('comments') or []
            summary=posts[i%len(posts)]['text'] if posts else ghl.caption(p,c)
            follow_up=body.follow_up or (comments[i%len(comments)]['text'] if comments else '')
        items.append(GhlPost(account_ids=body.account_ids,summary=summary,follow_up=follow_up,schedule_date=ghl.stagger(body.start,i,body.interval_hours),type=body.type,status='scheduled',user_id=body.user_id))
    def work(progress):
        for i,(c,item) in enumerate(zip(clips,items)):
            publish_clip(p,c,item,lambda m,f:progress(f'Post {i+1}/{len(clips)} · {m}',(i+f)/len(clips)))
    return queue(pid,'publish',work)

# An import that survived only on disk was interrupted by an engine restart.
for pending_file in DATA.glob('*/project.json'):
    pending=json.loads(pending_file.read_text(encoding='utf-8'))
    if pending.get('state')=='importing':
        pending['state']='error';pending['error']='Import was interrupted. Retry the URL, or upload the original video again.';write(pending)

app.mount('/',StaticFiles(directory=ROOT/'extension'/'studio',html=True),name='studio')
