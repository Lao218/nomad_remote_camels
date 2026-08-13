"""The OASIS-side, authenticated proxy and setup UI for CAMELS."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import quote
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


class ProtocolParameters(BaseModel):
    """Parameter values to persist as the CAMELS defaults for one protocol."""

    variables: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(BaseModel):
    """A user-owned CAMELS Action started from the Dashboard."""

    protocol_name: str = Field(min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)


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


SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CAMELS connections setup</title><style>body{font-family:Arial,sans-serif;max-width:720px;margin:3rem auto;line-height:1.45}label{display:block;font-weight:600;margin-top:1rem}input{box-sizing:border-box;width:100%;padding:.6rem;margin-top:.3rem}button{padding:.5rem .8rem;margin:.5rem}#saved{list-style:disc;padding-left:1.6rem;margin:.5rem 0}#saved li{display:flex;align-items:center;gap:.5rem;min-height:2.2rem;padding:.2rem .4rem;border-bottom:1px solid #ddd}#saved .delete{float:none;margin:0 0 0 auto;padding:.25rem .55rem;line-height:1.2;white-space:nowrap}pre{white-space:pre-wrap}</style></head><body><h1>CAMELS connections setup</h1><p>Paste a protocol-scoped CAMELS API key. A key for an existing protocol replaces that protocol's saved key; a new protocol is added to the list.</p><form id="form"><label>CAMELS API URL<input id="url" required value="http://host.docker.internal:8000"></label><label>CAMELS API key<input id="key" type="password" required autocomplete="off"></label><button type="button" id="test">Test connection</button><button type="submit">Save connection</button><a href="./dashboard">Go to dashboard</a></form><pre id="result"></pre><h2>Saved protocols</h2><ul id="saved"><li>Loading...</li></ul><script>const result=document.getElementById('result'),form=document.getElementById('form'),saved=document.getElementById('saved');function values(){return{camels_base_url:document.getElementById('url').value,api_key:document.getElementById('key').value}}function show(data,ok=true){result.textContent=(ok?'Success: ':'Error: ')+JSON.stringify(data,null,2)}async function request(path,options={}){const r=await fetch(path,options),data=await r.json();if(!r.ok)throw data;return data}async function refresh(){try{const data=await request('./settings/connections');saved.innerHTML='';const entries=data.connections||[];if(!entries.length){saved.innerHTML='<li>No protocol API keys saved.</li>';return}for(const entry of entries){const li=document.createElement('li');li.textContent=entry.name;const button=document.createElement('button');button.className='delete';button.textContent='Delete';button.onclick=async()=>{if(!confirm('Delete the saved key for '+entry.name+'?'))return;try{await request('./settings/connections/'+encodeURIComponent(entry.connection_id),{method:'DELETE'});refresh()}catch(e){show(e,false)}};li.append(button);saved.append(li)}}catch(e){show(e,false)}}document.getElementById('test').addEventListener('click',async()=>{try{show(await request('./settings/connection/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())}))}catch(e){show(e,false)}});form.addEventListener('submit',async e=>{e.preventDefault();try{const data=await request('./settings/connection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())});show(data);document.getElementById('key').value='';refresh()}catch(e){show(e,false)}});refresh();</script></body></html>"""

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
function renderPlotFrames(){
  browserPlotFrames.innerHTML='';
  for(const plot of browserPlots.filter(item=>item.shown)){
    const section=document.createElement('section'),heading=document.createElement('h3'),label=document.createElement('label'),select=document.createElement('select'),manual=document.createElement('button'),message=document.createElement('span'),connection=document.createElement('small'),frame=document.createElement('iframe');
    heading.textContent=plot.name+' (port '+plot.port+')';
    label.textContent='Clear mode ';
    select.innerHTML='<option value="automatic">Automatic</option><option value="manual">Manual</option>';
    select.value=plot.clearMode;
    manual.type='button';manual.textContent='Clear plot';manual.style.display=plot.clearMode==='manual'?'inline-block':'none';
    message.className='hint';message.style.marginLeft='.5rem';
    connection.className='browser-plot-connect-status hint';connection.style.display='none';
    frame.src=plotUrl(plot);frame.title=plot.name;frame.dataset.browserPlotKey=plotKey(plot);frame.style.cssText='width:100%;height:620px;border:0;margin-top:1rem';
    select.addEventListener('change',()=>{plot.clearMode=select.value;manual.style.display=plot.clearMode==='manual'?'inline-block':'none'});
    manual.addEventListener('click',async()=>{await clearPlot(plot,message);frame.src=plotUrl(plot)});
    label.append(select);section.append(heading,label,manual,message,connection,frame);browserPlotFrames.append(section);
  }
}
async function clearPlot(plot,message){if(message)message.textContent=' Clearing...';try{const response=await fetch(plotBaseUrl(plot.port)+'/clear_data',{method:'POST'});if(!response.ok)throw new Error('CAMELS browser plot returned HTTP '+response.status);if(message)message.textContent=' Cleared'}catch(error){if(message)message.textContent=' Could not clear plot: '+error.message}}
async function loadBrowserPlots(){try{const definition=await api('protocols/'+encodeURIComponent(protocol.value)+'/definition');browserPlots=collectBrowserPlots(definition);renderPlotTable();renderPlotFrames()}catch(error){browserPlots=[];renderPlotTable();browserPlotFrames.innerHTML='';status.textContent='Error loading browser plots: '+error.message}}
document.getElementById('load').addEventListener('click',loadBrowserPlots);
browserPlotUrlInput.addEventListener('change',()=>renderPlotFrames());
document.getElementById('add-manual-plot').addEventListener('click',()=>{const name=manualPlotName.value.trim(),port=Number(manualPlotPort.value);if(!name||!Number.isInteger(port)||port<1||port>65535){manualPlotStatus.textContent=' Enter a plot name and a valid port (1â€“65535).';return}const key=name+'@'+port;if(browserPlots.some(plot=>plotKey(plot)===key)){manualPlotStatus.textContent=' This plot is already in the list.';return}browserPlots.push({name,port,enabled:true,shown:true,clearMode:'automatic'});manualPlotName.value='';manualPlotPort.value='';manualPlotStatus.textContent=' Added.';renderPlotTable();renderPlotFrames()});
protocol.addEventListener('change',()=>{browserPlots=[];renderPlotTable();browserPlotFrames.innerHTML=''});
run.addEventListener('click',()=>{const automatic=browserPlots.filter(plot=>plot.shown&&plot.clearMode==='automatic');Promise.all(automatic.map(plot=>clearPlot(plot))).finally(()=>{if(browserPlots.some(plot=>plot.shown))setTimeout(renderPlotFrames,800)})});
</script>"""

# CAMELS main-window plot state is separate from browser/Dash plots.  This panel
# mirrors the native "Close plots" button through the authenticated bridge.
DASHBOARD_PAGE += """<script>
document.getElementById('browser-plots').insertAdjacentHTML('beforebegin','<section id="camels-plot-window-status"><h2>CAMELS plot windows</h2><p><strong id="camels-plot-window-count">Loadingâ€¦</strong> native CAMELS plot window(s) open.</p><button id="close-camels-plots" type="button">Close plots</button><span id="camels-plot-window-message" class="hint" style="margin-left:.5rem"></span></section>');
const camelsPlotWindowCount=document.getElementById('camels-plot-window-count'),closeCamelsPlotsButton=document.getElementById('close-camels-plots'),camelsPlotWindowMessage=document.getElementById('camels-plot-window-message');
function selectedPlotProtocol(){const name=protocol.value;return name&&name!=='Loading protocols...'&&name!=='No saved protocols'?name:''}
async function refreshCamelsPlotWindows(){const name=selectedPlotProtocol();if(!name){camelsPlotWindowCount.textContent='â€”';closeCamelsPlotsButton.disabled=true;return}try{const data=await api('plots?protocol_name='+encodeURIComponent(name));const count=Number(data.open_plot_count||0);camelsPlotWindowCount.textContent=String(count);closeCamelsPlotsButton.disabled=count===0;camelsPlotWindowMessage.textContent=''}catch(error){camelsPlotWindowCount.textContent='Unavailable';closeCamelsPlotsButton.disabled=true;camelsPlotWindowMessage.textContent=' '+error.message}}
closeCamelsPlotsButton.addEventListener('click',async()=>{const name=selectedPlotProtocol();if(!name)return;closeCamelsPlotsButton.disabled=true;camelsPlotWindowMessage.textContent=' Closingâ€¦';try{const data=await api('plots/close?protocol_name='+encodeURIComponent(name),{method:'POST'});camelsPlotWindowMessage.textContent=' Close requested for '+String(data.closed_plot_count||0)+' plot(s).';setTimeout(refreshCamelsPlotWindows,300)}catch(error){camelsPlotWindowMessage.textContent=' Could not close plots: '+error.message}finally{setTimeout(refreshCamelsPlotWindows,700)}});
protocol.addEventListener('change',refreshCamelsPlotWindows);const nativePlotWindowInterval=setInterval(refreshCamelsPlotWindows,2000);setTimeout(refreshCamelsPlotWindows,500);
</script>"""

# PID controller plots are produced inside the driver (not as Bluesky/Dash
# plots).  The API supplies thread-safe data snapshots and Plotly renders them
# in OASIS without exposing CAMELS credentials to the browser.
DASHBOARD_PAGE += """<script>
document.getElementById('browser-plots').insertAdjacentHTML('beforebegin','<section id="pid-controller-plots"><h2>PID controller plots</h2><p class="hint" id="pid-plot-status">Choose a protocol to inspect active PID controllers.</p><div id="pid-plot-frames"></div></section>');
const pidPlotStatus=document.getElementById('pid-plot-status'),pidPlotFrames=document.getElementById('pid-plot-frames');let pidPlotlyPromise=null;
function ensurePlotly(){if(window.Plotly)return Promise.resolve(window.Plotly);if(pidPlotlyPromise)return pidPlotlyPromise;pidPlotlyPromise=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='https://cdn.plot.ly/plotly-2.35.2.min.js';script.onload=()=>resolve(window.Plotly);script.onerror=()=>reject(new Error('Could not load Plotly.'));document.head.append(script)});return pidPlotlyPromise}
function pidPlotId(name){return 'pid-plot-'+name.replace(/[^a-zA-Z0-9_-]/g,'_')}
function ensurePidFrame(plot){const id=pidPlotId(plot.device_name);let frame=document.getElementById(id);if(frame)return frame;const section=document.createElement('section');section.id=id;const title=document.createElement('h3');title.textContent=plot.device_name+' â€” '+(plot.title||'PID plot');frame=document.createElement('div');frame.className='pid-plot-frame';frame.style.cssText='width:100%;height:260px;max-height:260px;overflow:hidden;margin-bottom:1rem';section.append(title,frame);pidPlotFrames.append(section);return frame}
async function refreshPidPlots(){const name=selectedPlotProtocol();if(!name){pidPlotStatus.textContent='Choose a protocol to inspect active PID controllers.';pidPlotFrames.innerHTML='';return}try{const data=await api('pid-plots?protocol_name='+encodeURIComponent(name)),plots=data.pid_plots||[];if(!plots.length){pidPlotStatus.textContent='No active PID controller plot is available for this protocol.';pidPlotFrames.innerHTML='';return}pidPlotStatus.textContent=plots.length+' active PID controller plot(s).';const active=new Set(plots.map(plot=>pidPlotId(plot.device_name)));for(const node of [...pidPlotFrames.children])if(!active.has(node.id))node.remove();const Plotly=await ensurePlotly();for(const plot of plots){const frame=ensurePidFrame(plot),series=plot.series||{},x=plot.x||[];const traces=['current value','setpoint','output','k','i','d'].filter(key=>Array.isArray(series[key])).map(key=>({x,y:series[key],name:key,mode:'lines',line:{width:key==='current value'||key==='setpoint'?3:1},visible:['current value','setpoint'].includes(key)?true:'legendonly',yaxis:['output','k','i','d'].includes(key)?'y2':'y'}));Plotly.react(frame,traces,{margin:{t:20,r:55,b:45,l:55},xaxis:{title:'time (s)'},yaxis:{title:'temperature / value'},yaxis2:{title:'PID values',overlaying:'y',side:'right'},legend:{orientation:'h'}},{responsive:true,displaylogo:false})}}catch(error){pidPlotStatus.textContent='Could not load PID plots: '+error.message}}
protocol.addEventListener('change',refreshPidPlots);const pidRefreshInterval=setInterval(refreshPidPlots,1000);setTimeout(refreshPidPlots,750);
</script>"""


# GUI v2's dashboard iframe intentionally does not grant ``allow-modals``.
# Replace the browser-native confirm() used by the setup page with a visible,
# two-click confirmation that works both embedded and as a standalone page.
SETUP_PAGE = SETUP_PAGE.replace(
    "if(!confirm('Delete the saved key for '+entry.name+'?'))return;",
    "if(button.dataset.confirming!=='true'){button.dataset.confirming='true';button.textContent='Confirm delete';setTimeout(()=>{button.dataset.confirming='';button.textContent='Delete'},4000);return;}",
)

# The two GUI v2 dashboards deliberately have separate responsibilities.
# Leave connection management in the setup dashboard and open the monitor
# dashboard in the parent GUI v2 window instead of navigating inside its iframe.
SETUP_PAGE = SETUP_PAGE.replace(
    '<a href="./dashboard">Go to dashboard</a>',
    '<a href="/nomad-oasis/gui/v2/dashboard_tools/camels-measurement-dashboard" target="_blank" rel="noopener">Go to dashboard</a>',
)

# Bulk deletion uses the same visible two-click confirmation pattern as each
# individual Delete button, which also works inside the GUI v2 iframe.
SETUP_PAGE = SETUP_PAGE.replace(
    "<h2>Saved protocols</h2>",
    '<div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap"><h2 style="margin:.8rem 0">Saved protocols</h2><button type="button" id="delete-all-protocols" class="delete">Delete all protocols</button></div>',
).replace(
    "refresh();</script>",
    """document.getElementById('delete-all-protocols').onclick=async()=>{const button=document.getElementById('delete-all-protocols');if(button.dataset.confirming!=='true'){button.dataset.confirming='true';button.textContent='Confirm delete all';setTimeout(()=>{button.dataset.confirming='';button.textContent='Delete all protocols'},4000);return;}try{await request('./settings/connections',{method:'DELETE'});button.dataset.confirming='';button.textContent='Delete all protocols';refresh()}catch(e){show(e,false)}};refresh();</script>""",
)

# Keep protocol execution close to the protocol-scoped API-key management.  The
# browser talks only to the OASIS bridge: the corresponding CAMELS key remains
# in the server-side credential store.
SETUP_PAGE = SETUP_PAGE.replace(
    "</body></html>",
    """<section id="protocol-control">
<style>
#protocol-control{margin-top:2rem;padding-top:1.2rem;border-top:1px solid #bbb}
#protocol-control select{min-width:240px;padding:.45rem;margin:.4rem .35rem .4rem 0}
#protocol-control button{margin:.4rem .35rem .4rem 0}
#protocol-control label{display:block;font-weight:600;margin-top:.7rem}
#protocol-control input{display:block;box-sizing:border-box;width:100%;padding:.5rem;margin-top:.25rem;font-weight:400}
#protocol-control .hint{color:#555}
#protocol-control .run-status{white-space:pre-wrap;margin-top:.6rem}
</style>
<h2>Protocol control</h2>
<label>Protocol<br><select id="remote-protocol"><option>Loading saved protocols...</option></select></label>
<button type="button" id="view-remote-protocol">View protocol</button>
<p class="hint">Values below apply only to the next measurement run; they do not overwrite the protocol saved in CAMELS.</p>
<h2>Measurement parameters</h2>
<div id="remote-parameters">Choose a protocol, then select View protocol.</div>
<button type="button" id="save-remote-parameters" disabled>Save parameters into protocol</button>
<button type="button" id="start-remote-measurement" disabled>Start measurement</button>
<div id="remote-run-status" class="run-status"></div>
<script>
const remoteProtocol=document.getElementById('remote-protocol');
const remoteParameters=document.getElementById('remote-parameters');
const remoteViewButton=document.getElementById('view-remote-protocol');
const remoteSaveButton=document.getElementById('save-remote-parameters');
const remoteStartButton=document.getElementById('start-remote-measurement');
const remoteRunStatus=document.getElementById('remote-run-status');
let remoteRunTimer=null;
async function remoteApi(path,options={}){
  const response=await fetch('./dashboard/'+path,options);
  const text=await response.text();
  let data;
  try{data=JSON.parse(text)}catch{data={detail:text}}
  if(!response.ok)throw new Error(data.detail||text);
  return data;
}
function remoteValue(value){
  if(value&&typeof value==='object'&&'default' in value)value=value.default;
  return typeof value==='object'?JSON.stringify(value):value??'';
}
async function loadRemoteProtocols(){
  try{
    const current=remoteProtocol.value;
    const data=await remoteApi('protocols');
    const names=data.Protocols||data.protocols||[];
    remoteProtocol.innerHTML='';
    for(const name of names){
      const option=document.createElement('option');
      option.value=option.textContent=name;
      remoteProtocol.append(option);
    }
    if(!names.length)remoteProtocol.innerHTML='<option>No saved protocols</option>';
    else if(names.includes(current))remoteProtocol.value=current;
  }catch(error){
    remoteProtocol.innerHTML='<option>Could not load protocols</option>';
    remoteRunStatus.textContent='Error: '+error.message;
  }
}
async function viewRemoteProtocol(){
  const name=remoteProtocol.value;
  if(!name||name==='No saved protocols'||name==='Could not load protocols')return;
  remoteViewButton.disabled=true;
  remoteStartButton.disabled=true;
  remoteParameters.textContent='Loading protocol parameters...';
  try{
    const schema=await remoteApi('protocols/'+encodeURIComponent(name)+'/schema');
    const definitions=schema.parameters||schema.variables||{};
    remoteParameters.innerHTML='';
    for(const [parameter,definition] of Object.entries(definitions)){
      const label=document.createElement('label');
      const input=document.createElement('input');
      label.textContent=parameter;
      input.name=parameter;
      input.value=remoteValue(definition);
      label.append(input);
      remoteParameters.append(label);
    }
    if(!Object.keys(definitions).length)remoteParameters.textContent='This protocol does not declare user parameters.';
    remoteRunStatus.textContent='';
    remoteSaveButton.disabled=false;
    remoteStartButton.disabled=false;
  }catch(error){
    remoteParameters.textContent='Could not load protocol parameters.';
    remoteRunStatus.textContent='Error: '+error.message;
  }finally{remoteViewButton.disabled=false;}
}
function remoteVariables(){
  const variables={};
  for(const input of remoteParameters.querySelectorAll('input')){
    let value=input.value;
    try{value=JSON.parse(value)}catch{}
    variables[input.name]=value;
  }
  return variables;
}
async function saveRemoteParameters(){
  const name=remoteProtocol.value;
  if(!name)return;
  remoteSaveButton.disabled=true;
  remoteRunStatus.textContent='Saving parameters into CAMELS protocol...';
  try{
    await remoteApi('protocols/'+encodeURIComponent(name)+'/parameters',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({variables:remoteVariables()})});
    // Read the schema back from CAMELS. This is both visual confirmation and
    // a guard against presenting a successful save when CAMELS rejected it.
    const verified=await remoteApi('protocols/'+encodeURIComponent(name)+'/schema');
    const persisted=verified.parameters||verified.variables||{};
    for(const input of remoteParameters.querySelectorAll('input')){
      if(Object.prototype.hasOwnProperty.call(persisted,input.name))input.value=remoteValue(persisted[input.name]);
    }
    remoteRunStatus.textContent='Parameters saved and verified in CAMELS protocol.';
  }catch(error){
    remoteRunStatus.textContent='Could not save parameters: '+error.message;
  }finally{remoteSaveButton.disabled=false;}
}
function remoteStatusInfo(result){
  const raw=String(result.status||result.state||'').trim();
  const value=raw.toLowerCase();
  if(raw.includes('/')||raw.includes('\\\\')||value.includes('finished')||value.includes('complete')||value.includes('success'))return ['Finished',true];
  if(value.includes('queue'))return ['Queued',false];
  if(value.includes('running')||!value)return ['Running',false];
  if(value.includes('failed')||value.includes('error')||value.includes('abort')||value.includes('stopped'))return ['Failed',true];
  return ['Running',false];
}
async function pollRemoteRun(runId,connectionId){
  clearTimeout(remoteRunTimer);
  try{
    const result=await remoteApi('runs/'+encodeURIComponent(runId)+'?connection_id='+encodeURIComponent(connectionId||''));
    const [text,done]=remoteStatusInfo(result);
    remoteRunStatus.textContent=text;
    if(done)remoteStartButton.disabled=false;
    else remoteRunTimer=setTimeout(()=>pollRemoteRun(runId,connectionId),1500);
  }catch(error){
    remoteRunStatus.textContent='Status check failed: '+error.message;
    remoteStartButton.disabled=false;
  }
}
async function startRemoteMeasurement(){
  const name=remoteProtocol.value;
  if(!name)return;
  remoteStartButton.disabled=true;
  remoteRunStatus.textContent='Starting measurement...';
  try{
    const result=await remoteApi('actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:name,variables:remoteVariables()})});
    const workflowId=result.workflow_id;
    if(workflowId){remoteRunStatus.textContent='Action queued';pollRemoteAction(workflowId)}
    else{remoteRunStatus.textContent='Action created';remoteStartButton.disabled=false;}
  }catch(error){
    remoteRunStatus.textContent='Failed: '+error.message;
    remoteStartButton.disabled=false;
  }
}
async function pollRemoteAction(workflowId){
  clearTimeout(remoteRunTimer);
  try{
    const result=await remoteApi('actions/'+encodeURIComponent(workflowId));
    const status=String(result.status||'PENDING').toUpperCase();
    remoteRunStatus.textContent='NOMAD Action: '+status;
    if(['COMPLETED','FAILED','CANCELLED','TERMINATED','TIMED_OUT'].includes(status))remoteStartButton.disabled=false;
    else remoteRunTimer=setTimeout(()=>pollRemoteAction(workflowId),1500);
  }catch(error){
    remoteRunStatus.textContent='Action status check failed: '+error.message;
    remoteStartButton.disabled=false;
  }
}
remoteViewButton.addEventListener('click',viewRemoteProtocol);
remoteSaveButton.addEventListener('click',saveRemoteParameters);
remoteStartButton.addEventListener('click',startRemoteMeasurement);
remoteProtocol.addEventListener('change',()=>{remoteSaveButton.disabled=true;remoteStartButton.disabled=true;remoteParameters.textContent='Choose a protocol, then select View protocol.';remoteRunStatus.textContent='';});
loadRemoteProtocols();
</script></section></body></html>""",
)

# Saving a new protocol-scoped key changes the catalogue used by the protocol
# control below. Refresh that selector immediately instead of requiring a page
# reload.
SETUP_PAGE = SETUP_PAGE.replace(
    "document.getElementById('key').value='';refresh()",
    "document.getElementById('key').value='';await refresh();await loadRemoteProtocols()",
)
DASHBOARD_PAGE = DASHBOARD_PAGE.replace(
    '<p><a href="./setup">CAMELS connections setup</a></p>',
    '',
)

# A CAMELS measurement can briefly make its local API unavailable while the
# protocol is starting or finishing. Keep polling such transient failures so a
# completed run is not incorrectly presented as a failed measurement.
DASHBOARD_PAGE += """<script>
let camelsStatusUnavailableRetries=0;
async function poll(id,connectionId){
  clearTimeout(timer);
  try{
    const result=await api('runs/'+encodeURIComponent(id)+'?connection_id='+encodeURIComponent(connectionId));
    const [text,done]=statusInfo(result);
    status.textContent=text;
    camelsStatusUnavailableRetries=0;
    if(done)run.disabled=false;
    else timer=setTimeout(()=>poll(id,connectionId),1500);
  }catch(error){
    const message=String(error.message||error);
    if(message.includes('CAMELS service is unavailable')&&camelsStatusUnavailableRetries<20){
      camelsStatusUnavailableRetries+=1;
      status.textContent='CAMELS connection temporarily unavailable; retrying ('+camelsStatusUnavailableRetries+'/20)...';
      timer=setTimeout(()=>poll(id,connectionId),2000);
      return;
    }
    status.textContent='Status check failed: '+message;
    run.disabled=false;
  }
}
</script>"""

# The monitoring dashboard intentionally has no execution controls.  Protocol
# parameters and Start measurement live in the connections dashboard; here a
# protocol selection immediately loads its sequence and the configured browser
# plots.  Detailed plot configuration remains available on demand.
DASHBOARD_PAGE += """<script>
(() => {
  const loadButton=document.getElementById('load');
  const parameterTitle=document.getElementById('parameters').previousElementSibling;
  const measurementHint=document.querySelector('p.hint');
  const runTitle=document.getElementById('status').previousElementSibling;
  loadButton.style.display='none';
  parameterTitle.style.display='none';
  document.getElementById('parameters').style.display='none';
  measurementHint.style.display='none';
  document.getElementById('run').style.display='none';
  runTitle.style.display='none';
  document.getElementById('status').style.display='none';

  const pidSection=document.getElementById('pid-controller-plots');
  if(pidSection){
    const pidHeading=pidSection.querySelector('h2');
    if(pidHeading)pidHeading.style.display='none';
    pidPlotStatus.style.display='none';
  }
  const nativePlotSection=document.getElementById('camels-plot-window-status');
  if(nativePlotSection)nativePlotSection.style.display='';

  const browserSection=document.getElementById('browser-plots');
  const browserHeading=browserSection.querySelector('h2');
  if(browserHeading)browserHeading.style.display='none';
  const plotTitle=document.createElement('h2');
  plotTitle.textContent='PLOT';
  plotTitle.style.marginBottom='.35rem';
  const advanced=document.createElement('section');
  advanced.id='advanced-plot-settings';
  advanced.style.cssText='margin:.8rem 0';
  const toggle=document.createElement('input');
  toggle.type='checkbox';
  toggle.id='advanced-plot-settings-toggle';
  const toggleLabel=document.createElement('label');
  toggleLabel.htmlFor=toggle.id;
  toggleLabel.textContent=' Advanced plot display settings';
  const settings=document.createElement('div');
  settings.hidden=true;
  settings.style.cssText='margin:.6rem 0;padding:.6rem;border:1px solid #ddd';
  const urlLabel=document.getElementById('browser-plot-url').closest('label');
  const manualControls=document.getElementById('manual-plot-name').closest('div');
  const plotTable=document.getElementById('browser-plot-rows').closest('table');
  settings.append(urlLabel,manualControls,plotTable);
  toggle.addEventListener('change',()=>{settings.hidden=!toggle.checked});
  function updateClearModeVisibility(){
    for(const label of browserPlotFrames.querySelectorAll('label')){
      if(!label.textContent.trim().startsWith('Clear mode'))continue;
      label.hidden=!toggle.checked;
      const clearButton=label.nextElementSibling;
      if(clearButton&&clearButton.tagName==='BUTTON')clearButton.hidden=!toggle.checked;
    }
  }
  toggle.addEventListener('change',updateClearModeVisibility);
  new MutationObserver(updateClearModeVisibility).observe(browserPlotFrames,{childList:true,subtree:true});
  updateClearModeVisibility();
  advanced.append(toggle,toggleLabel,settings);
  browserSection.insertBefore(plotTitle,document.getElementById('browser-plot-frames'));
  browserSection.insertBefore(advanced,document.getElementById('browser-plot-frames'));
  // Keep PID data plots, but present them as ordinary plots under PLOT rather
  // than as a separate controller-status panel.
  if(pidSection){
    browserSection.insertBefore(pidPlotFrames,document.getElementById('browser-plot-frames'));
    pidSection.style.display='none';
  }

  let lastLoadedProtocol='';
  async function automaticallyLoadProtocol(){
    const name=protocol.value;
    if(!name||name==='Loading protocols...'||name==='No saved protocols'||name==='Could not load protocols')return;
    if(name===lastLoadedProtocol)return;
    lastLoadedProtocol=name;
    await viewProtocol();
    await loadBrowserPlots();
  }
  protocol.addEventListener('change',()=>{lastLoadedProtocol='';automaticallyLoadProtocol()});
  const protocolObserver=new MutationObserver(()=>automaticallyLoadProtocol());
  protocolObserver.observe(protocol,{childList:true});
  setTimeout(automaticallyLoadProtocol,350);
})();
</script>"""

# Mirror the progress bar from the CAMELS main window next to the selected
# protocol. The data is a plain-Python snapshot served by CAMELS, so this
# remains safe even while the GUI is executing a measurement.
DASHBOARD_PAGE += """<script>
(() => {
  const protocolSelect=document.getElementById('protocol');
  if(!protocolSelect || document.getElementById('camels-measurement-progress'))return;
  const protocolLabel=protocolSelect.closest('label');
  if(!protocolLabel)return;
  const row=document.createElement('div');
  // Align the action with the bottom of the protocol select, rather than the
  // label above it. The status panel is stretched back to the row top below.
  row.style.cssText='display:flex;align-items:flex-end;gap:1.2rem;flex-wrap:wrap;margin:.25rem 0 .8rem';
  protocolLabel.parentNode.insertBefore(row,protocolLabel);
  row.append(protocolLabel);
  const startButton=document.createElement('button');
  startButton.type='button';
  startButton.id='dashboard-start-measurement';
  startButton.textContent='Start measurement';
  row.append(startButton);
  const panel=document.createElement('section');
  panel.id='camels-measurement-progress';
  panel.style.cssText='align-self:stretch;min-width:250px;border-left:1px solid #ddd;padding-left:1rem';
  panel.innerHTML='<strong>Measurement status</strong><div id="camels-measurement-status-text" class="hint">Idle</div><div style="height:12px;background:#e6e6e6;border-radius:6px;overflow:hidden;margin:.3rem 0"><div id="camels-measurement-progress-bar" style="height:100%;width:0%;background:#1976d2;transition:width .2s"></div></div><small id="camels-measurement-progress-detail">0%</small>';
  row.append(panel);
  const text=document.getElementById('camels-measurement-status-text');
  const bar=document.getElementById('camels-measurement-progress-bar');
  const detail=document.getElementById('camels-measurement-progress-detail');
  let actionTimer=null;
  let actionPending=false;
  let measurementRunning=false;
  function updateStartAvailability(){
    startButton.disabled=actionPending || measurementRunning;
    if(measurementRunning && !actionPending)startButton.textContent='Measurement running';
    else if(!actionPending)startButton.textContent='Start measurement';
  }
  function duration(seconds){
    if(seconds===null || seconds===undefined || !Number.isFinite(Number(seconds)))return 'â€”';
    seconds=Math.max(0,Math.round(Number(seconds)));
    const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=seconds%60;
    return h ? String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0') : String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
  }
  async function refreshMeasurementProgress(){
    const selected=protocolSelect.value;
    if(!selected || /^Loading|^No saved|^Could not load/.test(selected))return;
    try{
      const data=await api('measurement-status?protocol_name='+encodeURIComponent(selected));
      const status=String(data.status||'Idle');
      const progress=Math.max(0,Math.min(100,Number(data.progress)||0));
      const normalized=status.toLowerCase();
      measurementRunning=['running','preparing','queued'].some(word=>normalized.includes(word));
      const activeProtocol=data.protocol_name ? String(data.protocol_name) : '';
      text.textContent=measurementRunning && activeProtocol
        ? status+' â€” currently running: '+activeProtocol
        : status;
      bar.style.width=progress.toFixed(1)+'%';
      updateStartAvailability();
      detail.textContent=progress.toFixed(0)+'% Â· '+duration(data.elapsed_seconds)+' elapsed Â· '+(status==='Finished' ? 'finished' : duration(data.remaining_seconds)+' remaining');
    }catch(error){
      text.textContent='Status unavailable';
      detail.textContent=String(error.message||error);
    }
  }
  async function pollDashboardAction(workflowId){
    clearTimeout(actionTimer);
    try{
      const result=await api('actions/'+encodeURIComponent(workflowId));
      const actionStatus=String(result.status||'PENDING').toUpperCase();
      startButton.textContent='Action: '+actionStatus;
      if(['COMPLETED','FAILED','CANCELLED','TERMINATED','TIMED_OUT'].includes(actionStatus)){
        actionPending=false;
        updateStartAvailability();
      }else actionTimer=setTimeout(()=>pollDashboardAction(workflowId),1500);
    }catch(error){
      actionPending=false;
      updateStartAvailability();
      detail.textContent='Action status unavailable: '+String(error.message||error);
    }
  }
  startButton.addEventListener('click',async()=>{
    const name=protocolSelect.value;
    if(!name || /^Loading|^No saved|^Could not load/.test(name))return;
    actionPending=true;
    updateStartAvailability();
    startButton.textContent='Creating Action...';
    try{
      const values=typeof variables==='function' ? variables() : {};
      const result=await api('actions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol_name:name,variables:values})});
      if(result.workflow_id)pollDashboardAction(result.workflow_id);
      else{actionPending=false;updateStartAvailability();}
    }catch(error){
      actionPending=false;
      updateStartAvailability();
      detail.textContent='Could not create NOMAD Action: '+String(error.message||error);
    }
  });
  protocolSelect.addEventListener('change',refreshMeasurementProgress);
  setInterval(refreshMeasurementProgress,1000);
  setTimeout(refreshMeasurementProgress,500);
})();
</script>"""

# Replace browser/Dash iframes with direct snapshots of CAMELS' currently open
# native plot windows. This keeps all rendering in the OASIS page and updates
# figures in place, using the same data-snapshot pattern as PID monitoring.
DASHBOARD_PAGE += """<script>
(() => {
  // Native Qt window mirroring was intentionally withdrawn: it introduced
  // unstable cross-thread HTTP calls. Keep the proven Browser iframe and PID
  // snapshot paths defined above.
  return;
  clearInterval(nativePlotWindowInterval);
  clearInterval(pidRefreshInterval);
  const oldBrowserPlots=document.getElementById('browser-plots');
  const oldPidPlots=document.getElementById('pid-controller-plots');
  const oldNativePlots=document.getElementById('camels-plot-window-status');
  if(oldPidPlots)oldPidPlots.remove();
  if(oldNativePlots)oldNativePlots.remove();
  const nativePlots=document.createElement('section');
  nativePlots.id='native-live-plots';
  nativePlots.innerHTML='<div style="display:flex;align-items:center;gap:.7rem;flex-wrap:wrap"><h2 style="margin:.6rem 0">PLOT</h2><small id="native-plot-count">0 CAMELS plot window(s) open.</small><button type="button" id="native-close-plots">Close plots</button><small id="native-plot-message" class="hint"></small></div><div id="native-plot-frames"></div>';
  oldBrowserPlots.replaceWith(nativePlots);
  const nativeCount=document.getElementById('native-plot-count');
  const nativeCloseButton=document.getElementById('native-close-plots');
  const nativeMessage=document.getElementById('native-plot-message');
  const nativeFrames=document.getElementById('native-plot-frames');
  let nativePlotlyPromise=null;
  function ensureNativePlotly(){
    if(window.Plotly)return Promise.resolve(window.Plotly);
    if(nativePlotlyPromise)return nativePlotlyPromise;
    nativePlotlyPromise=new Promise((resolve,reject)=>{
      const script=document.createElement('script');
      script.src='https://cdn.plot.ly/plotly-2.35.2.min.js';
      script.onload=()=>resolve(window.Plotly);
      script.onerror=()=>reject(new Error('Could not load Plotly.'));
      document.head.append(script);
    });
    return nativePlotlyPromise;
  }
  function activeProtocolName(){
    const name=protocol.value;
    return name&&name!=='Loading protocols...'&&name!=='No saved protocols'&&name!=='Could not load protocols'?name:'';
  }
  function nativeFrameId(plot,index){return 'native-plot-'+String(plot.id||index).replace(/[^a-zA-Z0-9_-]/g,'_');}
  function nativeFrame(plot,index){
    const id=nativeFrameId(plot,index);
    let frame=document.getElementById(id);
    if(frame)return frame;
    const section=document.createElement('section');
    section.id=id;
    section.style.cssText='margin:1rem 0 2rem';
    const title=document.createElement('h3');
    title.className='native-plot-title';
    frame=document.createElement('div');
    frame.className='native-plot-frame';
    frame.style.cssText='width:100%;height:420px';
    section.append(title,frame);
    nativeFrames.append(section);
    return frame;
  }
  async function refreshNativePlots(){
    const protocolName=activeProtocolName();
    if(!protocolName){
      nativeCount.textContent='0 CAMELS plot window(s) open.';
      nativeCloseButton.disabled=true;
      nativeFrames.innerHTML='';
      return;
    }
    try{
      const data=await api('plot-snapshots?protocol_name='+encodeURIComponent(protocolName));
      const plots=data.plots||[];
      const count=Number(data.open_plot_count||0);
      nativeCount.textContent=count+' CAMELS plot window(s) open.';
      nativeCloseButton.disabled=count===0;
      nativeMessage.textContent='';
      const activeIds=new Set(plots.map((plot,index)=>nativeFrameId(plot,index)));
      for(const node of [...nativeFrames.children])if(!activeIds.has(node.id))node.remove();
      if(!plots.length){
        if(count)nativeMessage.textContent=' Some open windows do not provide a supported live data snapshot.';
        return;
      }
      const Plotly=await ensureNativePlotly();
      for(const [index,plot] of plots.entries()){
        const frame=nativeFrame(plot,index);
        frame.previousElementSibling.textContent=plot.title||('CAMELS plot '+(index+1));
        let traces,layout;
        if(plot.type==='scatter2d'){
          traces=[{x:plot.x||[],y:plot.y||[],mode:'markers',type:'scattergl',marker:{color:plot.z||[],colorscale:'Viridis',showscale:true,size:8,colorbar:{title:'z'}},name:plot.title||'data'}];
          layout={margin:{t:20,r:65,b:50,l:60},xaxis:{title:plot.x_label||'x'},yaxis:{title:plot.y_label||'y'}};
        }else{
          traces=(plot.traces||[]).map(trace=>({x:trace.x||[],y:trace.y||[],name:trace.name||'value',mode:'lines+markers',type:'scattergl',marker:{size:5},yaxis:trace.y_axis||'y'}));
          layout={margin:{t:20,r:60,b:50,l:60},xaxis:{title:plot.x_label||'x'},yaxis:{title:'value'},yaxis2:{title:'value (right axis)',overlaying:'y',side:'right'},legend:{orientation:'h'}};
        }
        Plotly.react(frame,traces,layout,{responsive:true,displaylogo:false});
      }
    }catch(error){
      nativeMessage.textContent=' Could not load CAMELS plot windows: '+error.message;
    }
  }
  nativeCloseButton.addEventListener('click',async()=>{
    const protocolName=activeProtocolName();
    if(!protocolName)return;
    nativeCloseButton.disabled=true;
    nativeMessage.textContent=' Closing plots...';
    try{
      await api('plots/close?protocol_name='+encodeURIComponent(protocolName),{method:'POST'});
      setTimeout(refreshNativePlots,300);
    }catch(error){
      nativeMessage.textContent=' Could not close plots: '+error.message;
    }
  });
  protocol.addEventListener('change',refreshNativePlots);
  setInterval(refreshNativePlots,1000);
  setTimeout(refreshNativePlots,500);
})();
</script>"""

# A Dash browser plot is launched only when CAMELS starts a run.  Browsers do
# not retry a failed iframe navigation once the Dash port later becomes ready.
# Probe the local port lightly and reload each iframe exactly once on an
# offline-to-online transition; this removes the need for a manual F5 without
# continuously rebuilding a live plot.
DASHBOARD_PAGE += """<script>
(() => {
  const browserPlotAvailability=new Map();
  async function browserPlotReachable(plot){
    try{
      await fetch(plotBaseUrl(plot.port)+'/',{mode:'no-cors',cache:'no-store'});
      return true;
    }catch(error){
      return false;
    }
  }
  async function retryBrowserPlotsWhenReady(){
    for(const plot of browserPlots.filter(item=>item.shown)){
      const key=plotKey(plot),available=await browserPlotReachable(plot),wasAvailable=browserPlotAvailability.get(key);
      browserPlotAvailability.set(key,available);
      const frame=browserPlotFrames.querySelector('iframe[data-browser-plot-key="'+CSS.escape(key)+'"]');
      if(available&&wasAvailable===false&&frame){
        frame.src=plotUrl(plot);
      }
    }
  }
  setInterval(retryBrowserPlotsWhenReady,1500);
  setTimeout(retryBrowserPlotsWhenReady,800);
})();
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
            detail = error_payload.get("detail", error_payload)
            if not isinstance(detail, str):
                detail = json.dumps(detail)
            raise HTTPException(status_code=exc.code, detail=detail) from exc
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

    @app.delete("/settings/connections")
    def delete_all_connections():
        """Remove all encrypted protocol keys from this local OASIS instance."""
        store.save({"connections": {}, "active_connection_id": None})
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

    @app.put("/dashboard/protocols/{protocol_name}/parameters")
    def dashboard_save_protocol_parameters(
        protocol_name: str, parameters: ProtocolParameters
    ):
        """Persist form values as defaults in the selected CAMELS protocol."""
        return request_camels(
            "PUT",
            f"/api/v1/protocols/{quote(protocol_name)}/parameters",
            payload={"variables": parameters.variables},
            connection=connection_for_protocol(protocol_name),
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

    @app.post("/dashboard/actions")
    def dashboard_start_action(
        request: ActionRequest, user=Depends(write_user)
    ):
        """Create a real NOMAD Action record and immediately start its workflow."""
        from nomad.actions.manager import start_action
        from nomad_camels_oasis.actions.models import CAMELSMeasurementInput

        # Check the protocol/key mapping before scheduling the asynchronous
        # workflow, so invalid dashboard requests fail immediately.
        connection_for_protocol(request.protocol_name)
        workflow_id = start_action(
            action_id="nomad_camels_oasis.entry_points:camels_measurement_action",
            data=CAMELSMeasurementInput(
                user_id=user.user_id,
                protocol_name=request.protocol_name,
                variables=request.variables,
            ),
        )
        return {"workflow_id": workflow_id, "status": "PENDING"}

    @app.get("/dashboard/actions/{workflow_id}")
    def dashboard_action_status(workflow_id: str, user=Depends(read_user)):
        """Return the status of the user-owned Action created by the dashboard."""
        from nomad.actions.manager import get_action_status

        return {
            "workflow_id": workflow_id,
            "status": get_action_status(workflow_id, user.user_id).name,
        }

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

    @app.get("/dashboard/plot-caches")
    def dashboard_plot_caches(protocol_name: str):
        """Proxy only Qt-independent 1D plot caches from CAMELS."""
        return request_camels(
            "GET",
            "/api/v1/gui/plot-caches",
            connection=connection_for_protocol(protocol_name),
        )

    @app.get("/dashboard/measurement-status")
    def dashboard_measurement_status(protocol_name: str):
        """Proxy CAMELS' thread-safe main-window progress snapshot."""
        return request_camels(
            "GET",
            "/api/v1/gui/measurement-status",
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
            f"/api/v1/gui/pid-plots?protocol_name={quote(protocol_name)}",
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
