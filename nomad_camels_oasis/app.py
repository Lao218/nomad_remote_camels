"""The OASIS-side, authenticated proxy and setup UI for CAMELS."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from nomad.app.v1.routers.auth import get_current_user
from nomad.auth.scopes import Scope


class RunRequest(BaseModel):
    protocol_name: str = Field(min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None


class ConnectionSettings(BaseModel):
    camels_base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    name: str = Field(default="")
    connection_id: str | None = None


class CredentialStore:
    """Persist the CAMELS key encrypted in the Oasis filesystem volume."""

    def __init__(self, path: str, encryption_secret: str | None):
        if not encryption_secret:
            raise RuntimeError("NOMAD_SERVICES_API_SECRET is required for CAMELS setup.")
        key = base64.urlsafe_b64encode(
            hashlib.sha256(encryption_secret.encode("utf-8")).digest()
        )
        self._fernet = Fernet(key)
        self._path = Path(path)

    def load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            encrypted = self._path.read_bytes()
            return json.loads(self._fernet.decrypt(encrypted).decode("utf-8"))
        except (InvalidToken, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=500,
                detail="Saved CAMELS connection settings cannot be read.",
            ) from exc

    def save(self, data: dict[str, Any]):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._fernet.encrypt(
            json.dumps(data).encode("utf-8")
        )
        temporary_path = self._path.with_suffix(".tmp")
        temporary_path.write_bytes(encrypted)
        os.replace(temporary_path, self._path)


SETUP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>CAMELS connection setup</title>
<style>body{font-family:Arial,sans-serif;max-width:720px;margin:3rem auto;line-height:1.45}label{display:block;font-weight:600;margin-top:1rem}input{box-sizing:border-box;width:100%;padding:.6rem;margin-top:.3rem}button{padding:.6rem 1rem;margin:.9rem .5rem .5rem 0}#result{white-space:pre-wrap}</style>
</head><body><h1>CAMELS connection setup</h1>
<p>Paste the protocol-scoped CAMELS API key that CAMELS copied to your clipboard. The key is encrypted before it is saved in the Oasis persistent volume.</p>
<p>The CAMELS API key is verified before it is saved. No separate OASIS token is required.</p>
<form id="form"><label>CAMELS API URL<input id="url" required value="http://host.docker.internal:8000"></label>
<label>CAMELS API key<input id="key" type="password" required autocomplete="off"></label>
<button type="button" onclick="testConnection()">Test connection</button><button type="submit">Save connection</button><a class="button" href="./dashboard">Go to dashboard</a></form><pre id="result"></pre>
<script>
const result=document.getElementById('result'); const form=document.getElementById('form');
function values(){return {camels_base_url:document.getElementById('url').value,api_key:document.getElementById('key').value};}
async function send(path){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())});const data=await r.json();result.textContent=r.ok?'Success: '+JSON.stringify(data,null,2):'Error: '+JSON.stringify(data,null,2);return r.ok;}
async function testConnection(){await send('./settings/connection/test');}
form.addEventListener('submit',async e=>{e.preventDefault();if(await send('./settings/connection')){document.getElementById('key').value='';}});
</script></body></html>"""

DASHBOARD_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>CAMELS dashboard</title>
<style>body{font-family:Arial;max-width:1100px;margin:2rem auto}select,input,button{padding:.5rem;margin:.3rem}#params label{display:block}pre{background:#f5f5f5;padding:1rem;white-space:pre-wrap}iframe{width:100%;height:650px;border:0}</style></head>
<body><h1>CAMELS measurement dashboard_DEMO</h1><p><a href="./setup">Connection setup</a></p><select id="protocol"></select><button onclick="loadProtocol()">Load parameters & sequence</button><button onclick="startRun()">Start measurement</button><div id="params"></div><h3>Sequence</h3><pre id="sequence"></pre><h3>Run status</h3><pre id="status"></pre><div id="plot"></div>
<script>
let schema={};let runId=null;const p=document.getElementById('protocol');
async function api(path,opts={}){if(path.startsWith('./'))path='/nomad-oasis/camels/dashboard/'+path.slice(2);let r=await fetch(path,opts);let t=await r.text();if(!r.ok)throw Error(t);try{return JSON.parse(t)}catch{return t}}
async function loadList(){let d=await api('./protocols');p.innerHTML='';for(const n of d.Protocols||[]){let o=document.createElement('option');o.value=o.textContent=n;p.append(o)}}
function sequenceText(steps,level=0){if(!Array.isArray(steps))return '';return steps.map(s=>{let label=s.name||s.full_name||s.step_type||s.type||'Step';let children=s.children||s.sub_steps||[];return '  '.repeat(level)+'• '+label+(children.length?'\n'+sequenceText(children,level+1):'')}).join('\n')}
async function loadProtocol(){let n=p.value;if(!n)return;schema=await api('./protocols/'+encodeURIComponent(n)+'/schema');let def=await api('./protocols/'+encodeURIComponent(n)+'/definition');let box=document.getElementById('params');box.innerHTML='';for(const [k,v] of Object.entries(schema.parameters||{})){let l=document.createElement('label');l.textContent=k;let i=document.createElement('input');i.name=k;i.value=typeof v==='object'?JSON.stringify(v):v;i.dataset.original=typeof v;l.append(i);box.append(l)}let steps=def.loop_steps||def.sequence||[];document.getElementById('sequence').textContent=sequenceText(steps)||'No sequence steps configured.'}
async function startRun(){let variables={};for(const i of document.querySelectorAll('#params input')){let v=i.value;try{v=JSON.parse(v)}catch{}variables[i.name]=v}let d=await api('./runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:p.value,variables})});runId=d.run_id;poll()}
async function poll(){if(!runId)return;try{let d=await api('./runs/'+runId);document.getElementById('status').textContent=JSON.stringify(d,null,2);document.getElementById('plot').innerHTML='<iframe src="./runs/'+runId+'/plotly"></iframe>'}catch(e){document.getElementById('status').textContent=e}setTimeout(poll,1500)}
loadList().catch(e=>document.getElementById('status').textContent=e);</script></body></html>"""

# Keep this page dependency-free so it can run directly inside a local OASIS
# deployment. The bridge owns the CAMELS credential; the browser never sees it.
DASHBOARD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS measurement dashboard</title>
<style>body{font-family:Arial,sans-serif;max-width:1000px;margin:2rem auto;line-height:1.45}select,input,button{padding:.55rem;margin:.35rem 0}select{min-width:300px}button{margin-left:.4rem}.button{display:inline-block;padding:.6rem 1rem;margin:.9rem .5rem .5rem 0;background:#eee;color:#111;text-decoration:none;border:1px solid #999;border-radius:2px}#parameters label{display:block;font-weight:600;margin-top:.7rem}#parameters input{display:block;box-sizing:border-box;width:100%;font-weight:400}pre{background:#f5f5f5;border:1px solid #ddd;padding:1rem;white-space:pre-wrap}.hint{color:#555}</style></head><body>
<h1>CAMELS measurement dashboard</h1><p><a href="./setup">Connection setup</a></p><label for="protocol"><strong>Protocol</strong></label><br><select id="protocol"><option>Loading protocols...</option></select><button id="load">View protocol</button><p class="hint">Values below apply only to the next measurement run; they do not overwrite the protocol saved in CAMELS.</p><h2>Measurement parameters</h2><div id="parameters">Choose a protocol, then select <em>View protocol</em>.</div><button id="run" disabled>Start measurement</button><h2>Sequence</h2><pre id="sequence">Choose a protocol to show its configured sequence.</pre><h2>Run status</h2><pre id="status">No measurement has been started.</pre>
<script>
const protocol=document.getElementById('protocol'),params=document.getElementById('parameters'),sequence=document.getElementById('sequence'),status=document.getElementById('status'),run=document.getElementById('run');let timer=null;
async function api(path,options={}){const response=await fetch('./dashboard/'+path,options);const text=await response.text();let data;try{data=JSON.parse(text)}catch{data={detail:text}}if(!response.ok)throw new Error(data.detail||text);return data}
function asValue(value){if(value&&typeof value==='object'&&'default'in value)value=value.default;return typeof value==='object'?JSON.stringify(value):value??''}
function stepsOf(value){if(Array.isArray(value))return value;if(!value||typeof value!=='object')return[];for(const key of ['loop_steps','sequence','steps','sub_steps','children'])if(Array.isArray(value[key]))return value[key];return[]}
function sequenceText(steps,level=0){return steps.map(step=>{if(typeof step==='string')return '  '.repeat(level)+'- '+step;const label=step.name||step.full_name||step.step_type||step.type||step.description||'Step';const children=stepsOf(step);return '  '.repeat(level)+'- '+label+(children.length?'\\n'+sequenceText(children,level+1):'')}).join('\\n')}
async function loadProtocols(){try{const data=await api('protocols');const names=data.Protocols||data.protocols||[];protocol.innerHTML='';if(!names.length){protocol.innerHTML='<option>No protocols available for this API key</option>';return}for(const name of names){const option=document.createElement('option');option.value=name;option.textContent=name;protocol.append(option)}}catch(error){protocol.innerHTML='<option>Could not load protocols</option>';status.textContent='Error: '+error.message}}
async function viewProtocol(){const name=protocol.value;if(!name)return;try{const [schema,definition]=await Promise.all([api('protocols/'+encodeURIComponent(name)+'/schema'),api('protocols/'+encodeURIComponent(name)+'/definition')]);const definitions=schema.parameters||schema.variables||{};params.innerHTML='';for(const[key,value]of Object.entries(definitions)){const label=document.createElement('label');label.textContent=key;const input=document.createElement('input');input.name=key;input.value=asValue(value);label.append(input);params.append(label)}if(!Object.keys(definitions).length)params.textContent='This protocol does not declare user parameters.';sequence.textContent=sequenceText(stepsOf(definition))||'No sequence steps configured.';run.disabled=false}catch(error){params.textContent='Could not load protocol details.';sequence.textContent='Error: '+error.message;run.disabled=true}}
function variables(){const result={};for(const input of params.querySelectorAll('input')){let value=input.value;try{value=JSON.parse(value)}catch{}result[input.name]=value}return result}
function statusInfo(result){const raw=String(result.status||result.state||'').trim(),value=raw.toLowerCase(),isResultPath=raw.includes('/')||raw.includes('\\\\');if(isResultPath||value.includes('finished')||value.includes('complete')||value.includes('success'))return {text:'Finished',done:true};if(value.includes('queue'))return {text:'Queued',done:false};if(value.includes('running')||!value)return {text:'Running',done:false};if(value.includes('failed')||value.includes('error')||value.includes('abort')||value.includes('stopped'))return {text:'Failed',done:true};return {text:'Running',done:false}}
function showStatus(result){const info=statusInfo(result);status.textContent=info.text;return info.done}
async function startRun(){try{run.disabled=true;status.textContent='Starting measurement...';const result=await api('runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:protocol.value,variables:variables()})});const id=result.run_id||result.uuid||result.id;if(id){status.textContent='Running';poll(id)}else{status.textContent='Started';run.disabled=false}}catch(error){status.textContent='Failed: '+error.message;run.disabled=false}}
async function poll(id){clearTimeout(timer);try{const result=await api('runs/'+encodeURIComponent(id));if(showStatus(result))run.disabled=false;else timer=setTimeout(()=>poll(id),1500)}catch(error){status.textContent='Status check failed: '+error.message;run.disabled=false}}
document.getElementById('load').addEventListener('click',viewProtocol);run.addEventListener('click',startRun);loadProtocols();
</script></body></html>"""


SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS connection setup</title><style>body{font-family:Arial,sans-serif;max-width:760px;margin:3rem auto;line-height:1.45}label{display:block;font-weight:600;margin-top:.8rem}input,select{box-sizing:border-box;width:100%;padding:.6rem;margin-top:.25rem}button{padding:.6rem 1rem;margin:.9rem .4rem .4rem 0}pre{white-space:pre-wrap;background:#f5f5f5;padding:.8rem}</style></head><body><h1>CAMELS connections</h1><p>Save one protocol-scoped CAMELS API key per connection. Existing connections remain saved when you add another one.</p><label>Saved connection<select id="saved"></select></label><button type="button" id="new">New connection</button><button type="button" id="remove">Delete selected</button><form id="form"><input id="connection-id" type="hidden"><label>Connection name<input id="name" placeholder="e.g. IV sweep"></label><label>CAMELS API URL<input id="url" required value="http://host.docker.internal:8000"></label><label>CAMELS API key<input id="key" type="password" required autocomplete="off"></label><button type="button" id="test">Test connection</button><button type="submit">Save connection</button><a href="./dashboard">Go to dashboard</a></form><pre id="result"></pre><script>const saved=document.getElementById('saved'),result=document.getElementById('result'),id=document.getElementById('connection-id'),name=document.getElementById('name'),url=document.getElementById('url'),key=document.getElementById('key');let entries=[];function value(){return{connection_id:id.value||null,name:name.value,camels_base_url:url.value,api_key:key.value}}function show(data,ok=true){result.textContent=(ok?'Success: ':'Error: ')+JSON.stringify(data,null,2)}async function request(path,options={}){const r=await fetch(path,options),data=await r.json();if(!r.ok)throw data;return data}function selectEntry(){const item=entries.find(x=>x.connection_id===saved.value);if(!item)return;id.value=item.connection_id;name.value=item.name;url.value=item.camels_base_url;key.value='';result.textContent='Enter this connection\'s API key only when you want to replace it.'}async function refresh(selectedId){try{const data=await request('./settings/connections');entries=data.connections||[];saved.innerHTML='';for(const item of entries){const option=document.createElement('option');option.value=item.connection_id;option.textContent=item.name;option.selected=item.connection_id===(selectedId||data.active_connection_id);saved.append(option)}if(entries.length)selectEntry();else newConnection()}catch(e){show(e,false)}}function newConnection(){id.value='';name.value='';url.value='http://host.docker.internal:8000';key.value='';saved.value='';result.textContent='Enter a new protocol API key, then test and save it.'}saved.addEventListener('change',selectEntry);document.getElementById('new').addEventListener('click',newConnection);document.getElementById('test').addEventListener('click',async()=>{try{show(await request('./settings/connection/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value())}))}catch(e){show(e,false)}});document.getElementById('form').addEventListener('submit',async e=>{e.preventDefault();try{const data=await request('./settings/connection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value())});show(data);key.value='';await refresh(data.connection_id)}catch(e){show(e,false)}});document.getElementById('remove').addEventListener('click',async()=>{if(!id.value||!confirm('Delete this saved CAMELS connection?'))return;try{await request('./settings/connections/'+encodeURIComponent(id.value),{method:'DELETE'});await refresh()}catch(e){show(e,false)}});refresh();</script></body></html>"""

DASHBOARD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS measurement dashboard</title><style>body{font-family:Arial,sans-serif;max-width:1000px;margin:2rem auto;line-height:1.45}select,input,button{padding:.55rem;margin:.35rem 0}select{min-width:300px}button{margin-left:.4rem}#parameters label{display:block;font-weight:600;margin-top:.7rem}#parameters input{display:block;box-sizing:border-box;width:100%;font-weight:400}pre{background:#f5f5f5;border:1px solid #ddd;padding:1rem;white-space:pre-wrap}.hint{color:#555}</style></head><body><h1>CAMELS measurement dashboard</h1><p><a href="./setup">Manage connections</a></p><label><strong>CAMELS connection</strong><br><select id="connection"><option>Loading connections...</option></select></label><br><label><strong>Protocol</strong><br><select id="protocol"><option>Select a connection first</option></select></label><button id="load">View protocol</button><p class="hint">Values below apply only to the next measurement run; they do not overwrite the protocol saved in CAMELS.</p><h2>Measurement parameters</h2><div id="parameters">Choose a connection and protocol.</div><button id="run" disabled>Start measurement</button><h2>Sequence</h2><pre id="sequence">Choose a protocol to show its configured sequence.</pre><h2>Run status</h2><pre id="status">No measurement has been started.</pre><script>const connection=document.getElementById('connection'),protocol=document.getElementById('protocol'),params=document.getElementById('parameters'),sequence=document.getElementById('sequence'),status=document.getElementById('status'),run=document.getElementById('run');let timer=null;function cid(){return connection.value}async function api(path,options={}){const r=await fetch('./dashboard/'+path,options),text=await r.text();let data;try{data=JSON.parse(text)}catch{data={detail:text}}if(!r.ok)throw new Error(data.detail||text);return data}function qs(){return '?connection_id='+encodeURIComponent(cid())}function stepsOf(v){if(Array.isArray(v))return v;if(!v||typeof v!=='object')return[];for(const k of ['loop_steps','sequence','steps','sub_steps','children'])if(Array.isArray(v[k]))return v[k];return[]}function sequenceText(steps,l=0){return steps.map(s=>{if(typeof s==='string')return '  '.repeat(l)+'- '+s;const label=s.name||s.full_name||s.step_type||s.type||s.description||'Step',children=stepsOf(s);return '  '.repeat(l)+'- '+label+(children.length?'\\n'+sequenceText(children,l+1):'')}).join('\\n')}function value(v){if(v&&typeof v==='object'&&'default'in v)v=v.default;return typeof v==='object'?JSON.stringify(v):v??''}async function loadConnections(){try{const data=await api('connections');connection.innerHTML='';for(const item of data.connections||[]){const o=document.createElement('option');o.value=item.connection_id;o.textContent=item.name;connection.append(o)}if(!connection.options.length){connection.innerHTML='<option>No saved connections</option>';return}loadProtocols()}catch(e){status.textContent='Error: '+e.message}}async function loadProtocols(){run.disabled=true;params.textContent='Choose a protocol, then select View protocol.';sequence.textContent='Choose a protocol to show its configured sequence.';try{const data=await api('protocols'+qs());protocol.innerHTML='';for(const n of data.Protocols||data.protocols||[]){const o=document.createElement('option');o.value=o.textContent=n;protocol.append(o)}if(!protocol.options.length)protocol.innerHTML='<option>No protocols available for this API key</option>'}catch(e){status.textContent='Error: '+e.message}}async function viewProtocol(){try{const n=protocol.value,[schema,definition]=await Promise.all([api('protocols/'+encodeURIComponent(n)+'/schema'+qs()),api('protocols/'+encodeURIComponent(n)+'/definition'+qs())]),defs=schema.parameters||schema.variables||{};params.innerHTML='';for(const[k,v]of Object.entries(defs)){const label=document.createElement('label'),input=document.createElement('input');label.textContent=k;input.name=k;input.value=value(v);label.append(input);params.append(label)}if(!Object.keys(defs).length)params.textContent='This protocol does not declare user parameters.';sequence.textContent=sequenceText(stepsOf(definition))||'No sequence steps configured.';run.disabled=false}catch(e){status.textContent='Error: '+e.message;run.disabled=true}}function variables(){const result={};for(const i of params.querySelectorAll('input')){let v=i.value;try{v=JSON.parse(v)}catch{}result[i.name]=v}return result}function statusInfo(r){const raw=String(r.status||r.state||'').trim(),v=raw.toLowerCase();if(raw.includes('/')||raw.includes('\\\\')||v.includes('finished')||v.includes('complete')||v.includes('success'))return['Finished',true];if(v.includes('queue'))return['Queued',false];if(v.includes('running')||!v)return['Running',false];if(v.includes('failed')||v.includes('error')||v.includes('abort')||v.includes('stopped'))return['Failed',true];return['Running',false]}async function poll(id){clearTimeout(timer);try{const r=await api('runs/'+encodeURIComponent(id)+qs()),[text,done]=statusInfo(r);status.textContent=text;if(done)run.disabled=false;else timer=setTimeout(()=>poll(id),1500)}catch(e){status.textContent='Status check failed: '+e.message;run.disabled=false}}async function startRun(){try{run.disabled=true;status.textContent='Starting measurement...';const r=await api('runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:protocol.value,variables:variables(),connection_id:cid()})}),id=r.run_id||r.uuid||r.id;if(id){status.textContent='Running';poll(id)}else{status.textContent='Started';run.disabled=false}}catch(e){status.textContent='Failed: '+e.message;run.disabled=false}}connection.addEventListener('change',loadProtocols);document.getElementById('load').addEventListener('click',viewProtocol);run.addEventListener('click',startRun);loadConnections();</script></body></html>"""


SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS connections setup</title><style>body{font-family:Arial,sans-serif;max-width:720px;margin:3rem auto;line-height:1.45}label{display:block;font-weight:600;margin-top:1rem}input{box-sizing:border-box;width:100%;padding:.6rem;margin-top:.3rem}button{padding:.6rem 1rem;margin:.9rem .5rem .5rem 0}pre{white-space:pre-wrap}</style></head><body><h1>CAMELS connections setup</h1><p>Paste the protocol-scoped CAMELS API key copied by CAMELS. It is encrypted before it is saved in the OASIS persistent volume.</p><form id="form"><label>CAMELS API URL<input id="url" required value="http://host.docker.internal:8000"></label><label>CAMELS API key<input id="key" type="password" required autocomplete="off"></label><button type="button" id="test">Test connection</button><button type="submit">Save connection</button><a href="./dashboard">Go to dashboard</a></form><pre id="result"></pre><script>const result=document.getElementById('result'),form=document.getElementById('form');function values(){return{camels_base_url:document.getElementById('url').value,api_key:document.getElementById('key').value}}async function send(path){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())}),data=await r.json();result.textContent=(r.ok?'Success: ':'Error: ')+JSON.stringify(data,null,2);return r.ok}document.getElementById('test').addEventListener('click',()=>send('./settings/connection/test'));form.addEventListener('submit',async e=>{e.preventDefault();if(await send('./settings/connection'))document.getElementById('key').value=''})</script></body></html>"""

DASHBOARD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS measurement dashboard</title><style>body{font-family:Arial,sans-serif;max-width:1000px;margin:2rem auto;line-height:1.45}select,input,button{padding:.55rem;margin:.35rem 0}select{min-width:300px}button{margin-left:.4rem}#parameters label{display:block;font-weight:600;margin-top:.7rem}#parameters input{display:block;box-sizing:border-box;width:100%;font-weight:400}pre{background:#f5f5f5;border:1px solid #ddd;padding:1rem;white-space:pre-wrap}.hint{color:#555}</style></head><body><h1>CAMELS measurement dashboard</h1><p><a href="./setup">CAMELS connections setup</a></p><label><strong>Protocol</strong><br><select id="protocol"><option>Loading protocols...</option></select></label><button id="load">View protocol</button><p class="hint">Values below apply only to the next measurement run; they do not overwrite the protocol saved in CAMELS.</p><h2>Measurement parameters</h2><div id="parameters">Choose a protocol, then select View protocol.</div><button id="run" disabled>Start measurement</button><h2>Sequence</h2><pre id="sequence">Choose a protocol to show its configured sequence.</pre><h2>Run status</h2><pre id="status">No measurement has been started.</pre><script>const protocol=document.getElementById('protocol'),params=document.getElementById('parameters'),sequence=document.getElementById('sequence'),status=document.getElementById('status'),run=document.getElementById('run');let timer=null;async function api(path,options={}){const r=await fetch('./dashboard/'+path,options),text=await r.text();let data;try{data=JSON.parse(text)}catch{data={detail:text}}if(!r.ok)throw new Error(data.detail||text);return data}function stepsOf(v){if(Array.isArray(v))return v;if(!v||typeof v!=='object')return[];for(const k of ['loop_steps','sequence','steps','sub_steps','children'])if(Array.isArray(v[k]))return v[k];return[]}function sequenceText(steps,l=0){return steps.map(s=>{if(typeof s==='string')return '  '.repeat(l)+'- '+s;const label=s.name||s.full_name||s.step_type||s.type||s.description||'Step',children=stepsOf(s);return '  '.repeat(l)+'- '+label+(children.length?'\\n'+sequenceText(children,l+1):'')}).join('\\n')}function value(v){if(v&&typeof v==='object'&&'default'in v)v=v.default;return typeof v==='object'?JSON.stringify(v):v??''}async function loadProtocols(){try{const data=await api('protocols');protocol.innerHTML='';for(const n of data.Protocols||data.protocols||[]){const o=document.createElement('option');o.value=o.textContent=n;protocol.append(o)}if(!protocol.options.length)protocol.innerHTML='<option>No protocols available for this API key</option>'}catch(e){protocol.innerHTML='<option>Could not load protocols</option>';status.textContent='Error: '+e.message}}async function viewProtocol(){try{const n=protocol.value,[schema,definition]=await Promise.all([api('protocols/'+encodeURIComponent(n)+'/schema'),api('protocols/'+encodeURIComponent(n)+'/definition')]),defs=schema.parameters||schema.variables||{};params.innerHTML='';for(const[k,v]of Object.entries(defs)){const label=document.createElement('label'),input=document.createElement('input');label.textContent=k;input.name=k;input.value=value(v);label.append(input);params.append(label)}if(!Object.keys(defs).length)params.textContent='This protocol does not declare user parameters.';sequence.textContent=sequenceText(stepsOf(definition))||'No sequence steps configured.';run.disabled=false}catch(e){status.textContent='Error: '+e.message;run.disabled=true}}function variables(){const result={};for(const i of params.querySelectorAll('input')){let v=i.value;try{v=JSON.parse(v)}catch{}result[i.name]=v}return result}function statusInfo(r){const raw=String(r.status||r.state||'').trim(),v=raw.toLowerCase();if(raw.includes('/')||raw.includes('\\\\')||v.includes('finished')||v.includes('complete')||v.includes('success'))return['Finished',true];if(v.includes('queue'))return['Queued',false];if(v.includes('running')||!v)return['Running',false];if(v.includes('failed')||v.includes('error')||v.includes('abort')||v.includes('stopped'))return['Failed',true];return['Running',false]}async function poll(id){clearTimeout(timer);try{const r=await api('runs/'+encodeURIComponent(id)),[text,done]=statusInfo(r);status.textContent=text;if(done)run.disabled=false;else timer=setTimeout(()=>poll(id),1500)}catch(e){status.textContent='Status check failed: '+e.message;run.disabled=false}}async function startRun(){try{run.disabled=true;status.textContent='Starting measurement...';const r=await api('runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:protocol.value,variables:variables()})}),id=r.run_id||r.uuid||r.id;if(id){status.textContent='Running';poll(id)}else{status.textContent='Started';run.disabled=false}}catch(e){status.textContent='Failed: '+e.message;run.disabled=false}}document.getElementById('load').addEventListener('click',viewProtocol);run.addEventListener('click',startRun);loadProtocols();</script></body></html>"""


SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS connections setup</title><style>body{font-family:Arial,sans-serif;max-width:720px;margin:3rem auto;line-height:1.45}label{display:block;font-weight:600;margin-top:1rem}input{box-sizing:border-box;width:100%;padding:.6rem;margin-top:.3rem}button{padding:.5rem .8rem;margin:.5rem}.delete{float:right}li{padding:.45rem;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}</style></head><body><h1>CAMELS connections setup</h1><p>Paste a protocol-scoped CAMELS API key. A key for an existing protocol replaces that protocol's saved key; a new protocol is added to the list.</p><form id="form"><label>CAMELS API URL<input id="url" required value="http://host.docker.internal:8000"></label><label>CAMELS API key<input id="key" type="password" required autocomplete="off"></label><button type="button" id="test">Test connection</button><button type="submit">Save connection</button><a href="./dashboard">Go to dashboard</a></form><pre id="result"></pre><h2>Saved protocols</h2><ul id="saved"><li>Loading...</li></ul><script>const result=document.getElementById('result'),form=document.getElementById('form'),saved=document.getElementById('saved');function values(){return{camels_base_url:document.getElementById('url').value,api_key:document.getElementById('key').value}}function show(data,ok=true){result.textContent=(ok?'Success: ':'Error: ')+JSON.stringify(data,null,2)}async function request(path,options={}){const r=await fetch(path,options),data=await r.json();if(!r.ok)throw data;return data}async function refresh(){try{const data=await request('./settings/connections');saved.innerHTML='';const entries=data.connections||[];if(!entries.length){saved.innerHTML='<li>No protocol API keys saved.</li>';return}for(const entry of entries){const li=document.createElement('li');li.textContent=entry.name;const button=document.createElement('button');button.className='delete';button.textContent='Delete';button.onclick=async()=>{if(!confirm('Delete the saved key for '+entry.name+'?'))return;try{await request('./settings/connections/'+encodeURIComponent(entry.connection_id),{method:'DELETE'});refresh()}catch(e){show(e,false)}};li.append(button);saved.append(li)}}catch(e){show(e,false)}}document.getElementById('test').addEventListener('click',async()=>{try{show(await request('./settings/connection/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())}))}catch(e){show(e,false)}});form.addEventListener('submit',async e=>{e.preventDefault();try{const data=await request('./settings/connection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())});show(data);document.getElementById('key').value='';refresh()}catch(e){show(e,false)}});refresh();</script></body></html>"""

DASHBOARD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS measurement dashboard</title><style>body{font-family:Arial,sans-serif;max-width:1000px;margin:2rem auto;line-height:1.45}select,input,button{padding:.55rem;margin:.35rem 0}select{min-width:300px}button{margin-left:.4rem}#parameters label{display:block;font-weight:600;margin-top:.7rem}#parameters input{display:block;box-sizing:border-box;width:100%;font-weight:400}pre{background:#f5f5f5;border:1px solid #ddd;padding:1rem;white-space:pre-wrap}.hint{color:#555}</style></head><body><h1>CAMELS measurement dashboard</h1><p><a href="./setup">CAMELS connections setup</a></p><label><strong>Protocol</strong><br><select id="protocol"><option>Loading protocols...</option></select></label><button id="load">View protocol</button><p class="hint">Values below apply only to the next measurement run; they do not overwrite the protocol saved in CAMELS.</p><h2>Measurement parameters</h2><div id="parameters">Choose a protocol, then select View protocol.</div><button id="run" disabled>Start measurement</button><h2>Sequence</h2><pre id="sequence">Choose a protocol to show its configured sequence.</pre><h2>Run status</h2><pre id="status">No measurement has been started.</pre><script>const protocol=document.getElementById('protocol'),params=document.getElementById('parameters'),sequence=document.getElementById('sequence'),status=document.getElementById('status'),run=document.getElementById('run');let timer=null;async function api(path,options={}){const r=await fetch('./dashboard/'+path,options),text=await r.text();let data;try{data=JSON.parse(text)}catch{data={detail:text}}if(!r.ok)throw new Error(data.detail||text);return data}function stepsOf(v){if(Array.isArray(v))return v;if(!v||typeof v!=='object')return[];for(const k of ['loop_steps','sequence','steps','sub_steps','children'])if(Array.isArray(v[k]))return v[k];return[]}function sequenceText(steps,l=0){return steps.map(s=>{if(typeof s==='string')return '  '.repeat(l)+'- '+s;const label=s.name||s.full_name||s.step_type||s.type||s.description||'Step',children=stepsOf(s);return '  '.repeat(l)+'- '+label+(children.length?'\\n'+sequenceText(children,l+1):'')}).join('\\n')}function value(v){if(v&&typeof v==='object'&&'default'in v)v=v.default;return typeof v==='object'?JSON.stringify(v):v??''}async function loadProtocols(){try{const data=await api('protocols');protocol.innerHTML='';for(const n of data.Protocols||[]){const o=document.createElement('option');o.value=o.textContent=n;protocol.append(o)}if(!protocol.options.length)protocol.innerHTML='<option>No saved protocols</option>'}catch(e){protocol.innerHTML='<option>Could not load protocols</option>';status.textContent='Error: '+e.message}}async function viewProtocol(){try{const n=protocol.value,[schema,definition]=await Promise.all([api('protocols/'+encodeURIComponent(n)+'/schema'),api('protocols/'+encodeURIComponent(n)+'/definition')]),defs=schema.parameters||schema.variables||{};params.innerHTML='';for(const[k,v]of Object.entries(defs)){const label=document.createElement('label'),input=document.createElement('input');label.textContent=k;input.name=k;input.value=value(v);label.append(input);params.append(label)}if(!Object.keys(defs).length)params.textContent='This protocol does not declare user parameters.';sequence.textContent=sequenceText(stepsOf(definition))||'No sequence steps configured.';run.disabled=false}catch(e){status.textContent='Error: '+e.message;run.disabled=true}}function variables(){const result={};for(const i of params.querySelectorAll('input')){let v=i.value;try{v=JSON.parse(v)}catch{}result[i.name]=v}return result}function statusInfo(r){const raw=String(r.status||r.state||'').trim(),v=raw.toLowerCase();if(raw.includes('/')||raw.includes('\\\\')||v.includes('finished')||v.includes('complete')||v.includes('success'))return['Finished',true];if(v.includes('queue'))return['Queued',false];if(v.includes('running')||!v)return['Running',false];if(v.includes('failed')||v.includes('error')||v.includes('abort')||v.includes('stopped'))return['Failed',true];return['Running',false]}async function poll(id,connectionId){clearTimeout(timer);try{const r=await api('runs/'+encodeURIComponent(id)+'?connection_id='+encodeURIComponent(connectionId)),[text,done]=statusInfo(r);status.textContent=text;if(done)run.disabled=false;else timer=setTimeout(()=>poll(id,connectionId),1500)}catch(e){status.textContent='Status check failed: '+e.message;run.disabled=false}}async function startRun(){try{run.disabled=true;status.textContent='Starting measurement...';const r=await api('runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:protocol.value,variables:variables()})}),id=r.run_id||r.uuid||r.id;if(id){status.textContent='Running';poll(id,r.connection_id)}else{status.textContent='Started';run.disabled=false}}catch(e){status.textContent='Failed: '+e.message;run.disabled=false}}document.getElementById('load').addEventListener('click',viewProtocol);run.addEventListener('click',startRun);loadProtocols();</script></body></html>"""

# Browser plot definitions are read from the selected CAMELS protocol.  The
# browser communicates with each local Dash service directly, so that each
# configured plot may use its own port.
DASHBOARD_PAGE += """<script>
document.body.insertAdjacentHTML('beforeend','<section id="browser-plots"><h2>Browser plots</h2><label>Browser URL: <input id="browser-plot-url" value="http://localhost:" style="width:250px"></label><div><label>Plot name: <input id="manual-plot-name" placeholder="Plot name" style="width:180px"></label><label>Port: <input id="manual-plot-port" type="number" min="1" max="65535" placeholder="8050" style="width:90px"></label><button id="add-manual-plot" type="button">Add plot</button><span id="manual-plot-status" class="hint"></span></div><table style="width:100%;border-collapse:collapse"><thead><tr><th align="left">Plot name</th><th align="left">Port</th><th align="left">Display below</th></tr></thead><tbody id="browser-plot-rows"><tr><td colspan="3">Choose View protocol to load its plots.</td></tr></tbody></table><div id="browser-plot-frames"></div></section>');
const browserPlotRows=document.getElementById('browser-plot-rows'),browserPlotFrames=document.getElementById('browser-plot-frames'),browserPlotUrlInput=document.getElementById('browser-plot-url'),manualPlotName=document.getElementById('manual-plot-name'),manualPlotPort=document.getElementById('manual-plot-port'),manualPlotStatus=document.getElementById('manual-plot-status');let browserPlots=[];
function plotKey(plot){return plot.name+'@'+plot.port}
function walkForPlots(value,result){if(!value||typeof value!=='object')return;if(Array.isArray(value)){for(const item of value)walkForPlots(item,result);return}if(value.checkbox_show_in_browser!==undefined&&value.browser_port!==undefined)result.push(value);for(const key of ['loop_steps','sequence','steps','sub_steps','children','plots'])if(value[key])walkForPlots(value[key],result)}
function collectBrowserPlots(definition){const found=[];walkForPlots(definition,found);const used=new Set();return found.map((item,index)=>({name:String(item.name||item.title||('Plot '+(index+1))),port:Number(item.browser_port),enabled:item.checkbox_show_in_browser===true,shown:item.checkbox_show_in_browser===true,clearMode:'automatic'})).filter(item=>Number.isInteger(item.port)&&item.port>0).filter(item=>{const key=plotKey(item);if(used.has(key))return false;used.add(key);return true})}
function plotBaseUrl(port){let base=browserPlotUrlInput.value.trim();while(base.endsWith('/'))base=base.slice(0,-1);return (base.endsWith(':')?base:base+':')+port}
function plotUrl(plot){return plotBaseUrl(plot.port)+'/?run='+Date.now()}
function renderPlotTable(){browserPlotRows.innerHTML='';if(!browserPlots.length){browserPlotRows.innerHTML='<tr><td colspan="3">This protocol has no configured browser plots.</td></tr>';return}for(const plot of browserPlots){const row=document.createElement('tr'),name=document.createElement('td'),port=document.createElement('td'),display=document.createElement('td'),box=document.createElement('input');name.textContent=plot.name;port.textContent=String(plot.port);box.type='checkbox';box.checked=plot.shown;box.disabled=!plot.enabled;box.title=plot.enabled?'':'Enable Show plot in browser for this plot in CAMELS first.';box.addEventListener('change',()=>{plot.shown=box.checked;renderPlotFrames()});display.append(box);row.append(name,port,display);browserPlotRows.append(row)}}
function renderPlotFrames(){browserPlotFrames.innerHTML='';for(const plot of browserPlots.filter(item=>item.shown)){const section=document.createElement('section'),heading=document.createElement('h3'),label=document.createElement('label'),select=document.createElement('select'),manual=document.createElement('button'),message=document.createElement('span'),frame=document.createElement('iframe');heading.textContent=plot.name+' (port '+plot.port+')';label.textContent='Clear mode ';select.innerHTML='<option value="automatic">Automatic</option><option value="manual">Manual</option>';select.value=plot.clearMode;manual.type='button';manual.textContent='Clear plot';manual.style.display=plot.clearMode==='manual'?'inline-block':'none';message.className='hint';message.style.marginLeft='.5rem';frame.src=plotUrl(plot);frame.title=plot.name;frame.style.cssText='width:100%;height:620px;border:0;margin-top:1rem';select.addEventListener('change',()=>{plot.clearMode=select.value;manual.style.display=plot.clearMode==='manual'?'inline-block':'none'});manual.addEventListener('click',async()=>{await clearPlot(plot,message);frame.src=plotUrl(plot)});label.append(select);section.append(heading,label,manual,message,frame);browserPlotFrames.append(section)}}
async function clearPlot(plot,message){if(message)message.textContent=' Clearing...';try{const response=await fetch(plotBaseUrl(plot.port)+'/clear_data',{method:'POST'});if(!response.ok)throw new Error('CAMELS browser plot returned HTTP '+response.status);if(message)message.textContent=' Cleared'}catch(error){if(message)message.textContent=' Could not clear plot: '+error.message}}
async function loadBrowserPlots(){try{const definition=await api('protocols/'+encodeURIComponent(protocol.value)+'/definition');browserPlots=collectBrowserPlots(definition);renderPlotTable();renderPlotFrames()}catch(error){browserPlots=[];renderPlotTable();browserPlotFrames.innerHTML='';status.textContent='Error loading browser plots: '+error.message}}
document.getElementById('load').addEventListener('click',loadBrowserPlots);
browserPlotUrlInput.addEventListener('change',()=>renderPlotFrames());
document.getElementById('add-manual-plot').addEventListener('click',()=>{const name=manualPlotName.value.trim(),port=Number(manualPlotPort.value);if(!name||!Number.isInteger(port)||port<1||port>65535){manualPlotStatus.textContent=' Enter a plot name and a valid port (1–65535).';return}const key=name+'@'+port;if(browserPlots.some(plot=>plotKey(plot)===key)){manualPlotStatus.textContent=' This plot is already in the list.';return}browserPlots.push({name,port,enabled:true,shown:true,clearMode:'automatic'});manualPlotName.value='';manualPlotPort.value='';manualPlotStatus.textContent=' Added.';renderPlotTable();renderPlotFrames()});
protocol.addEventListener('change',()=>{browserPlots=[];renderPlotTable();browserPlotFrames.innerHTML=''});
run.addEventListener('click',()=>{const automatic=browserPlots.filter(plot=>plot.shown&&plot.clearMode==='automatic');Promise.all(automatic.map(plot=>clearPlot(plot))).finally(()=>{if(browserPlots.some(plot=>plot.shown))setTimeout(renderPlotFrames,800)})});
</script>"""

# CAMELS main-window plot state is separate from browser/Dash plots.  This panel
# mirrors the native "Close plots" button through the authenticated bridge.
DASHBOARD_PAGE += """<script>
document.getElementById('browser-plots').insertAdjacentHTML('beforebegin','<section id="camels-plot-window-status"><h2>CAMELS plot windows</h2><p><strong id="camels-plot-window-count">Loading…</strong> native CAMELS plot window(s) open.</p><button id="close-camels-plots" type="button">Close plots</button><span id="camels-plot-window-message" class="hint" style="margin-left:.5rem"></span></section>');
const camelsPlotWindowCount=document.getElementById('camels-plot-window-count'),closeCamelsPlotsButton=document.getElementById('close-camels-plots'),camelsPlotWindowMessage=document.getElementById('camels-plot-window-message');
function selectedPlotProtocol(){const name=protocol.value;return name&&name!=='Loading protocols...'&&name!=='No saved protocols'?name:''}
async function refreshCamelsPlotWindows(){const name=selectedPlotProtocol();if(!name){camelsPlotWindowCount.textContent='—';closeCamelsPlotsButton.disabled=true;return}try{const data=await api('plots?protocol_name='+encodeURIComponent(name));const count=Number(data.open_plot_count||0);camelsPlotWindowCount.textContent=String(count);closeCamelsPlotsButton.disabled=count===0;camelsPlotWindowMessage.textContent=''}catch(error){camelsPlotWindowCount.textContent='Unavailable';closeCamelsPlotsButton.disabled=true;camelsPlotWindowMessage.textContent=' '+error.message}}
closeCamelsPlotsButton.addEventListener('click',async()=>{const name=selectedPlotProtocol();if(!name)return;closeCamelsPlotsButton.disabled=true;camelsPlotWindowMessage.textContent=' Closing…';try{const data=await api('plots/close?protocol_name='+encodeURIComponent(name),{method:'POST'});camelsPlotWindowMessage.textContent=' Close requested for '+String(data.closed_plot_count||0)+' plot(s).';setTimeout(refreshCamelsPlotWindows,300)}catch(error){camelsPlotWindowMessage.textContent=' Could not close plots: '+error.message}finally{setTimeout(refreshCamelsPlotWindows,700)}});
protocol.addEventListener('change',refreshCamelsPlotWindows);setInterval(refreshCamelsPlotWindows,2000);setTimeout(refreshCamelsPlotWindows,500);
</script>"""

# PID controller plots are produced inside the driver (not as Bluesky/Dash
# plots).  The API supplies thread-safe data snapshots and Plotly renders them
# in OASIS without exposing CAMELS credentials to the browser.
DASHBOARD_PAGE += """<script>
document.getElementById('browser-plots').insertAdjacentHTML('beforebegin','<section id="pid-controller-plots"><h2>PID controller plots</h2><p class="hint" id="pid-plot-status">Choose a protocol to inspect active PID controllers.</p><div id="pid-plot-frames"></div></section>');
const pidPlotStatus=document.getElementById('pid-plot-status'),pidPlotFrames=document.getElementById('pid-plot-frames');let pidPlotlyPromise=null;
function ensurePlotly(){if(window.Plotly)return Promise.resolve(window.Plotly);if(pidPlotlyPromise)return pidPlotlyPromise;pidPlotlyPromise=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='https://cdn.plot.ly/plotly-2.35.2.min.js';script.onload=()=>resolve(window.Plotly);script.onerror=()=>reject(new Error('Could not load Plotly.'));document.head.append(script)});return pidPlotlyPromise}
function pidPlotId(name){return 'pid-plot-'+name.replace(/[^a-zA-Z0-9_-]/g,'_')}
function ensurePidFrame(plot){const id=pidPlotId(plot.device_name);let frame=document.getElementById(id);if(frame)return frame;const section=document.createElement('section');section.id=id;const title=document.createElement('h3');title.textContent=plot.device_name+' — '+(plot.title||'PID plot');frame=document.createElement('div');frame.className='pid-plot-frame';frame.style.cssText='width:100%;height:420px;margin-bottom:1.5rem';section.append(title,frame);pidPlotFrames.append(section);return frame}
async function refreshPidPlots(){const name=selectedPlotProtocol();if(!name){pidPlotStatus.textContent='Choose a protocol to inspect active PID controllers.';pidPlotFrames.innerHTML='';return}try{const data=await api('pid-plots?protocol_name='+encodeURIComponent(name)),plots=data.pid_plots||[];if(!plots.length){pidPlotStatus.textContent='No active PID controller plot is available for this protocol.';pidPlotFrames.innerHTML='';return}pidPlotStatus.textContent=plots.length+' active PID controller plot(s).';const active=new Set(plots.map(plot=>pidPlotId(plot.device_name)));for(const node of [...pidPlotFrames.children])if(!active.has(node.id))node.remove();const Plotly=await ensurePlotly();for(const plot of plots){const frame=ensurePidFrame(plot),series=plot.series||{},x=plot.x||[];const traces=['current value','setpoint','output','k','i','d'].filter(key=>Array.isArray(series[key])).map(key=>({x,y:series[key],name:key,mode:'lines',line:{width:key==='current value'||key==='setpoint'?3:1},visible:['current value','setpoint'].includes(key)?true:'legendonly',yaxis:['output','k','i','d'].includes(key)?'y2':'y'}));Plotly.react(frame,traces,{margin:{t:20,r:55,b:45,l:55},xaxis:{title:'time (s)'},yaxis:{title:'temperature / value'},yaxis2:{title:'PID values',overlaying:'y',side:'right'},legend:{orientation:'h'}},{responsive:true,displaylogo:false})}}catch(error){pidPlotStatus.textContent='Could not load PID plots: '+error.message}}
protocol.addEventListener('change',refreshPidPlots);setInterval(refreshPidPlots,1000);setTimeout(refreshPidPlots,750);
</script>"""


def create_app(
    *,
    camels_base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    settings_path: str,
    encryption_secret: str | None,
) -> FastAPI:
    app = FastAPI(
        title="OASIS CAMELS bridge",
        version="1.1.0",
        description="Authenticated proxy from NOMAD Oasis to CAMELS.",
    )
    store = CredentialStore(settings_path, encryption_secret)
    default_url = camels_base_url.rstrip("/")

    def saved_connections() -> tuple[dict[str, dict[str, str]], str | None]:
        """Return the new multi-connection format and transparently read v1 data."""
        saved = store.load()
        connections = saved.get("connections")
        if isinstance(connections, dict):
            cleaned = {
                connection_id: value
                for connection_id, value in connections.items()
                if isinstance(value, dict) and value.get("api_key")
            }
            return cleaned, saved.get("active_connection_id")
        # Version 1 stored exactly one URL/key pair. Keep it available as a
        # migration entry without requiring the user to paste the key again.
        if saved.get("api_key"):
            return {
                "default": {
                    "name": saved.get("name") or "Existing CAMELS connection",
                    "camels_base_url": saved.get("camels_base_url", default_url),
                    "api_key": saved["api_key"],
                }
            }, "default"
        return {}, None

    def connection_summary() -> dict[str, Any]:
        connections, active_connection_id = saved_connections()
        return {
            "connections": [
                {
                    "connection_id": connection_id,
                    "name": value.get("name") or connection_id,
                    "camels_base_url": value.get("camels_base_url", default_url),
                }
                for connection_id, value in connections.items()
            ],
            "active_connection_id": active_connection_id,
        }

    def current_connection(connection_id: str | None = None) -> tuple[str, str]:
        connections, active_connection_id = saved_connections()
        selected_id = connection_id or active_connection_id
        selected = connections.get(selected_id) if selected_id else None
        if selected:
            return selected.get("camels_base_url", default_url).rstrip("/"), selected["api_key"]
        # Retain environment-configured deployments that do not use setup.
        url, key = default_url, api_key
        if not key:
            raise HTTPException(
                status_code=503,
                detail="CAMELS is not configured. Open /camels/setup and save a connection.",
            )
        return url, key

    def persist_connection(settings: ConnectionSettings) -> dict[str, Any]:
        connections, active_connection_id = saved_connections()
        connection_id = settings.connection_id if settings.connection_id in connections else uuid.uuid4().hex
        connections[connection_id] = {
            "name": settings.name.strip() or settings.camels_base_url.rstrip("/"),
            "camels_base_url": settings.camels_base_url.rstrip("/"),
            "api_key": settings.api_key,
        }
        store.save(
            {
                "connections": connections,
                "active_connection_id": connection_id or active_connection_id,
            }
        )
        return {"connection_id": connection_id, **connection_summary()}

    def save_protocol_connections(
        settings: ConnectionSettings, protocol_response: dict[str, Any]
    ) -> dict[str, Any]:
        """Store one encrypted credential per protocol, replacing name matches."""
        protocol_names = [
            str(name)
            for name in protocol_response.get("Protocols", [])
            if isinstance(name, str) and name
        ]
        if not protocol_names:
            raise HTTPException(
                status_code=400,
                detail="The CAMELS API key did not grant access to any protocol.",
            )
        connections, _ = saved_connections()
        # A protocol-scoped key is identified by the protocol it can control.
        # Replacing it never affects credentials for other protocol names.
        names_to_replace = set(protocol_names)
        connections = {
            connection_id: value
            for connection_id, value in connections.items()
            if value.get("protocol_name") not in names_to_replace
        }
        for protocol_name in protocol_names:
            connection_id = uuid.uuid4().hex
            connections[connection_id] = {
                "name": protocol_name,
                "protocol_name": protocol_name,
                "camels_base_url": settings.camels_base_url.rstrip("/"),
                "api_key": settings.api_key,
            }
        store.save(
            {
                "connections": connections,
                "active_connection_id": next(reversed(connections)),
            }
        )
        return connection_summary()

    def protocol_catalog() -> tuple[list[str], dict[str, str]]:
        """Return all saved protocol names and the credential that owns each."""
        connections, _ = saved_connections()
        names: list[str] = []
        connection_ids: dict[str, str] = {}
        for connection_id, value in connections.items():
            protocol_name = value.get("protocol_name")
            if protocol_name:
                if protocol_name not in connection_ids:
                    names.append(protocol_name)
                connection_ids[protocol_name] = connection_id
                continue
            # Compatibility for an old single-key record: discover its name.
            response = request_camels(
                "GET",
                "/api/v1/protocols",
                connection=(value.get("camels_base_url", default_url).rstrip("/"), value["api_key"]),
            )
            for discovered_name in response.get("Protocols", []):
                if discovered_name not in connection_ids:
                    names.append(discovered_name)
                    connection_ids[discovered_name] = connection_id
        return names, connection_ids

    def connection_for_protocol(protocol_name: str) -> tuple[str, str]:
        _, connection_ids = protocol_catalog()
        connection_id = connection_ids.get(protocol_name)
        if not connection_id:
            raise HTTPException(status_code=404, detail="No saved API key for this protocol.")
        return current_connection(connection_id)

    def request_camels(
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        connection: tuple[str, str] | None = None,
        require_key: bool = True,
    ) -> Any:
        if connection is None:
            connection = current_connection() if require_key else (default_url, "")
        base_url, key = connection
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                response_body = response.read()
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                error_payload = {"detail": "CAMELS returned a non-JSON error."}
            raise HTTPException(status_code=exc.code, detail=error_payload) from exc
        except URLError as exc:
            raise HTTPException(502, "CAMELS service is unavailable.") from exc
        try:
            return json.loads(response_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"detail": "CAMELS returned a non-JSON response."}

    write_user = get_current_user([Scope.ENTRIES_WRITE], allow_anonymous=False)
    read_user = get_current_user([Scope.ENTRIES_READ], allow_anonymous=False)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page():
        return SETUP_PAGE

    @app.get("/settings/connection")
    def connection_status(_user=Depends(write_user)):
        return {"configured": bool(saved_connections()[0] or api_key), **connection_summary()}

    @app.get("/settings/connections")
    def list_connections():
        protocol_names, connection_ids = protocol_catalog()
        return {
            "connections": [
                {"connection_id": connection_ids[name], "name": name}
                for name in protocol_names
            ]
        }

    @app.post("/settings/connection/test")
    def test_connection(settings: ConnectionSettings):
        return request_camels(
            "GET",
            "/api/v1/protocols",
            connection=(settings.camels_base_url.rstrip("/"), settings.api_key),
        )

    @app.post("/settings/connection")
    def save_connection(settings: ConnectionSettings):
        protocols = request_camels(
            "GET",
            "/api/v1/protocols",
            connection=(settings.camels_base_url.rstrip("/"), settings.api_key),
        )
        return {"saved": True, "protocols": protocols, **save_protocol_connections(settings, protocols)}

    @app.delete("/settings/connections/{connection_id}")
    def delete_connection(connection_id: str):
        connections, active_connection_id = saved_connections()
        if connection_id not in connections:
            raise HTTPException(status_code=404, detail="CAMELS connection not found.")
        del connections[connection_id]
        if active_connection_id == connection_id:
            active_connection_id = next(iter(connections), None)
        store.save({"connections": connections, "active_connection_id": active_connection_id})
        return connection_summary()

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page():
        return DASHBOARD_PAGE

    @app.get("/dashboard/connections")
    def dashboard_connections():
        return connection_summary()

    # The dashboard is deliberately served without an extra NOMAD API-token
    # challenge, consistent with the local setup page. CAMELS credentials remain
    # server-side in the encrypted bridge store.
    @app.get("/dashboard/protocols")
    def dashboard_protocols(connection_id: str | None = None):
        protocol_names, connection_ids = protocol_catalog()
        return {"Protocols": protocol_names, "connection_ids": connection_ids}

    @app.get("/dashboard/protocols/{protocol_name}/schema")
    def dashboard_protocol_schema(protocol_name: str, connection_id: str | None = None):
        return request_camels(
            "GET", f"/api/v1/protocols/{protocol_name}/schema", connection=connection_for_protocol(protocol_name)
        )

    @app.get("/dashboard/protocols/{protocol_name}/definition")
    def dashboard_protocol_definition(protocol_name: str, connection_id: str | None = None):
        return request_camels(
            "GET", f"/api/v1/protocols/{protocol_name}/JSON", connection=connection_for_protocol(protocol_name)
        )

    @app.post("/dashboard/runs")
    def dashboard_start_run(request: RunRequest):
        connection_ids = protocol_catalog()[1]
        response = request_camels(
            "POST",
            f"/api/v1/actions/run/protocols/{request.protocol_name}",
            payload={"variables": request.variables},
            connection=connection_for_protocol(request.protocol_name),
        )
        return {**response, "connection_id": connection_ids[request.protocol_name]}

    @app.get("/dashboard/runs/{run_id}")
    def dashboard_run_status(run_id: str, connection_id: str | None = None):
        return request_camels("GET", f"/api/v1/protocols/results/{run_id}", connection=current_connection(connection_id))

    @app.get("/dashboard/plots")
    def dashboard_plot_windows(protocol_name: str):
        """Return the live CAMELS GUI plot-window count for one saved protocol key."""
        return request_camels(
            "GET",
            "/api/v1/gui/plots",
            connection=connection_for_protocol(protocol_name),
        )

    @app.post("/dashboard/plots/close")
    def dashboard_close_plot_windows(protocol_name: str):
        """Request CAMELS to perform its main-window Close plots action."""
        return request_camels(
            "POST",
            "/api/v1/gui/plots/close",
            connection=connection_for_protocol(protocol_name),
        )

    @app.get("/dashboard/pid-plots")
    def dashboard_pid_plots(protocol_name: str):
        """Proxy the active PID controller traces for the selected protocol."""
        return request_camels(
            "GET",
            "/api/v1/gui/pid-plots",
            connection=connection_for_protocol(protocol_name),
        )

    @app.get("/health")
    def health(connection_id: str | None = None):
        url, _ = current_connection(connection_id)
        return request_camels(
            "GET",
            "/api/v1/health",
            connection=(url, ""),
            require_key=False,
        )

    @app.get("/protocols")
    def list_protocols(_user=Depends(read_user)):
        return request_camels("GET", "/api/v1/protocols")

    @app.get("/protocols/{protocol_name}/schema")
    def get_protocol_schema(protocol_name: str, _user=Depends(read_user)):
        return request_camels("GET", f"/api/v1/protocols/{protocol_name}/schema")

    @app.get("/protocols/{protocol_name}/definition")
    def get_protocol_definition(protocol_name: str, _user=Depends(read_user)):
        return request_camels("GET", f"/api/v1/protocols/{protocol_name}/JSON")

    @app.post("/runs")
    def start_run(request: RunRequest, _user=Depends(write_user)):
        return request_camels(
            "POST",
            f"/api/v1/actions/run/protocols/{request.protocol_name}",
            payload={"variables": request.variables},
        )

    @app.get("/runs/{run_id}")
    def get_run_status(run_id: str, _user=Depends(read_user)):
        return request_camels("GET", f"/api/v1/protocols/results/{run_id}")

    return app
