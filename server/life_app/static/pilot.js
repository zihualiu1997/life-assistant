let csrf='', initialized=false;
let revision='';
const $=s=>document.querySelector(s);
async function api(path,method='GET',body) { const response=await fetch(path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},...(body?{body:JSON.stringify(body)}:{})}); const data=await response.json(); if(!response.ok) throw Error(data.detail==='authentication_required'?'请先登录。':`操作未完成：${data.detail||response.status}`); return data; }
async function run(fn){try{await fn();}catch(error){$('#notice').textContent=error.message;}}
function ready(){$('#login').hidden=true;$('#workspace').hidden=false;$('#notice').textContent='已登录你的空间。';}
$('#login').onsubmit=event=>{event.preventDefault();run(async()=>{const result=await api(initialized?'/api/login':'/api/bootstrap','POST',{password:$('#password').value,token:$('#invite').value});csrf=result.csrf;$('#password').value='';$('#invite').value='';ready();});};
$('#request-email').onclick=()=>run(async()=>{await api('/api/email/request-verification','POST',{email:$('#email').value});$('#notice').textContent='验证邮件已提交，请检查邮箱。';});
$('#verify-email').onclick=()=>run(async()=>{await api('/api/email/verify','POST',{code:$('#code').value});$('#code').value='';$('#notice').textContent='邮箱已验证。';});
async function wechat(){const data=await api('/api/wechat/status');$('#wechat-state').textContent=data.status;$('#qr').hidden=!data.url;if(data.url)$('#qr').src='/api/wechat/qr';}
$('#wechat-login').onclick=()=>run(async()=>{await api('/api/wechat/login','POST',{});$('#notice').textContent='正在申请二维码，请稍后刷新连接状态。';await wechat();});
$('#wechat-refresh').onclick=()=>run(wechat);
$('#wechat-start').onclick=()=>run(async()=>{await api('/api/wechat/start','POST',{});$('#notice').textContent='已提交启动请求，请在微信发送测试消息确认。';});
document.querySelectorAll('[data-test]').forEach(button=>button.onclick=()=>run(async()=>{await api('/api/test/'+button.dataset.test,'POST',{});$('#notice').textContent=button.dataset.test==='mail'?'SMTP 已接受，请检查实际收件。':'模型测试通过。';}));
document.querySelectorAll('[data-preview]').forEach(button=>button.onclick=()=>run(async()=>{$('#preview').textContent=(await api('/api/preview/'+button.dataset.preview,'POST',{})).body;}));
$('#activate').onclick=()=>run(async()=>{await api('/api/activate','POST',{mail_received:$('#mail-received').checked,previews_approved:$('#preview-approved').checked});$('#notice').textContent='简报已启用。';});
$('#usage').onclick=()=>run(async()=>{$('#usage-text').textContent=JSON.stringify(await api('/api/usage'),null,2);});
$('#logout').onclick=()=>run(async()=>{await api('/api/logout','POST',{});location.reload();});
$('#notes-load').onclick=()=>run(async()=>{const data=await api('/v1/notes');$('#notes-list').replaceChildren(new Option('选择笔记',''),...data.items.map(name=>new Option(name,name)));});
$('#notes-list').onchange=()=>run(async()=>{const name=$('#notes-list').value;if(!name)return;const data=await api('/v1/note?path='+encodeURIComponent(name));$('#note-path').value=name;$('#note-text').value=data.text;revision=data.revision;});
$('#note-path').oninput=()=>{revision='';};
$('#note-save').onclick=()=>run(async()=>{const data=await api('/v1/note','PUT',{path:$('#note-path').value,text:$('#note-text').value,revision});revision=data.revision;$('#notice').textContent='笔记已保存。';});
run(async()=>{initialized=(await api('/api/bootstrap')).initialized;$('#invite-label').hidden=initialized;try{csrf=(await api('/api/session')).csrf;ready();}catch{$('#notice').textContent=initialized?'请登录。':'使用运营者分享的一次性凭据开通。';}});
