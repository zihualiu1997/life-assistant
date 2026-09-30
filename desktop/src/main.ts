import { invoke } from '@tauri-apps/api/core';
import { getCurrentWindow, LogicalSize } from '@tauri-apps/api/window';
import './style.css';
const $=<T extends HTMLElement>(s:string)=>document.querySelector<T>(s)!;
const input=(s:string)=>$<HTMLInputElement>(s).value;
const status=(s:string)=>$('#status').textContent=s;
let conversation=crypto.randomUUID(), pending:{id:string,body:string}|undefined, record:{id:string,date:string,body:string}|undefined, busy=false;
let expanded=false, frame=0, row=0;
const sequences:Record<number,number[]>={0:[280,110,110,140,140,320],3:[140,140,140,280],5:[140,140,140,140,140,140,140,240],6:[150,150,150,150,150,260],7:[120,120,120,120,120,220],8:[150,150,150,150,150,280]};
function animate(){ const durations=sequences[row]||sequences[0];frame%=durations.length;$('#sprite').style.backgroundPosition=`-${frame*84}px -${row*91}px`;const delay=durations[frame];frame=(frame+1)%durations.length;window.setTimeout(animate,delay) }
async function expand(value:boolean){expanded=value;$('main').hidden=!value;await getCurrentWindow().setSize(new LogicalSize(value?460:112,value?720:132))}
async function safe(fn:()=>Promise<void>){try{await fn()}catch(e){status(String(e));row=5}}
let down:{x:number,y:number}|undefined, dragged=false;
$('#pet').onpointerdown=e=>{down={x:e.screenX,y:e.screenY};dragged=false};
$('#pet').onpointermove=e=>{if(down&&!dragged&&Math.hypot(e.screenX-down.x,e.screenY-down.y)>5){dragged=true;down=undefined;void getCurrentWindow().startDragging()}};
$('#pet').onpointerup=()=>{if(!dragged)void safe(()=>expand(!expanded));down=undefined};
$('#collapse').onclick=()=>safe(()=>expand(false));
document.querySelectorAll<HTMLButtonElement>('[data-tab]').forEach(b=>b.onclick=()=>{if(busy)return;document.querySelectorAll('section').forEach(s=>s.hidden=s.id!==b.dataset.tab);status('')});
async function request(action:string,data:unknown={}){return invoke<any>('request',{action,data})}
$('#pair').onclick=()=>safe(async()=>{await invoke('pair',{endpoint:input('#endpoint'),code:input('#code')});$<HTMLInputElement>('#code').value='';status('已配对。设备凭据已保存到 Windows 凭据库。')});
$('#forget').onclick=()=>safe(async()=>{await invoke('forget');status('已清除本机连接。如需撤销设备，请在服务器网页操作。')});
$('#check').onclick=()=>safe(async()=>{await request('health');status('连接正常。')});
$('#send').onclick=()=>safe(async()=>{if(busy)return;const body=input('#question').trim();if(!body)return;if(pending&&pending.body!==body){status('上一条结果待确认，请先查历史，或点击新对话后重新输入。');return}pending??={id:crypto.randomUUID(),body};busy=true;row=7;status('正在回复…');try{const d=await request('chat',{message_id:pending.id,conversation_id:conversation,body});$<HTMLTextAreaElement>('#answer').value=d.reply;pending=undefined;row=8;status('已回复。')}finally{busy=false}});
$('#new').onclick=()=>{if(busy)return;conversation=crypto.randomUUID();pending=undefined;$<HTMLTextAreaElement>('#question').value='';$<HTMLTextAreaElement>('#answer').value='';row=0;status('新对话')};
$('#run').onclick=()=>safe(async()=>{if(busy)return;const action=input('#action'),date=input('#date'),body=input('#body');let data:any={date};if(action==='record'){if(record&&(record.date!==date||record.body!==body)){status('重试必须保留原日期和原文，或点击开始新记录。');return}record??={id:crypto.randomUUID(),date,body};data={date,body,message_id:record.id}}busy=true;try{const d=await request(action,data);$<HTMLTextAreaElement>('#result').value=action==='record'?`${d.status==='recorded'?'已保存':'此前已保存，未重复写入'}\n${d.source}`:(d.items||[]).map((x:any)=>`${x.source}\n${x.text||'该日期没有记录。'}`).join('\n\n');status('操作完成。')}finally{busy=false}});
$('#new-record').onclick=()=>{if(busy)return;record=undefined;$<HTMLTextAreaElement>('#body').value='';$<HTMLTextAreaElement>('#result').value='';status('可以开始另一条记录。')};
$('#refresh').onclick=()=>safe(async()=>{const d=await request('history');const list=$('#history-items');list.replaceChildren();for(const item of d.items){const article=document.createElement('article');const p=document.createElement('p');p.textContent=`你：${item.body}\n\n助手：${item.reply||'结果待确认'}\n${item.status}`;article.append(p);list.append(article)}});
const day=new Date();$<HTMLInputElement>('#date').value=`${day.getFullYear()}-${String(day.getMonth()+1).padStart(2,'0')}-${String(day.getDate()).padStart(2,'0')}`;
void safe(async()=>{const pet=await invoke<string>('pet');$('#sprite').style.backgroundImage=`url("data:image/webp;base64,${pet}")`;animate();const c=await invoke<any>('connection');$<HTMLInputElement>('#endpoint').value=c.endpoint||'';if(!c.configured){await expand(true);document.querySelectorAll('section').forEach(s=>s.hidden=s.id!=='connection')}});
