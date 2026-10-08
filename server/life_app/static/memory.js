let csrf = '', current = {};
const message = document.querySelector('#message');
async function api(path, method='GET', body) {
  const response = await fetch(path, {method, headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, ...(body ? {body:JSON.stringify(body)} : {})});
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 401 ? '请先回到首页登录。' : response.status === 409 ? '内容已发生变化，请刷新页面后再编辑。' : '保存未完成，请检查输入后重试。');
  return data;
}
async function run(fn) { try { await fn(); } catch(error) { message.textContent=error.message; } }
async function load() {
  csrf=(await api('/api/session')).csrf;
  current=await api('/api/preferences');
  for(const [key,value] of Object.entries(current)) {
    const input=document.querySelector(`[name="${key}"]`);
    if(input.type==='checkbox') input.checked=value; else input.value=value;
  }
  const {items}=await api('/api/memories');
  const list=document.querySelector('#memories'); list.replaceChildren();
  for(const item of items) {
    const box=document.createElement('article'); box.className='record';
    const source=document.createElement('p'); source.textContent=`${item.status} · ${item.source_kind} · ${new Date(item.source_at*1000).toLocaleString()}\n原话：${item.quote}`;
    const text=document.createElement('textarea'); text.value=item.text; text.maxLength=4000; text.setAttribute('aria-label','记忆内容');
    const save=document.createElement('button'); save.textContent='确认／纠正';
    const forget=document.createElement('button'); forget.textContent='撤回记忆'; forget.className='secondary';
    const update=async (remove)=>{ await api('/api/memories/'+encodeURIComponent(item.id),'PUT',{revision:item.revision,text:text.value,forget:remove}); await load(); message.textContent=remove?'记忆已撤回。':'记忆已更新。'; };
    save.onclick=()=>run(()=>update(false)); forget.onclick=()=>run(()=>update(true));
    box.append(source,text,save,forget); list.append(box);
  }
  if(!items.length) list.textContent='还没有保存的长期记忆。';
}
document.querySelector('#preferences').onsubmit=event=>{event.preventDefault();run(async()=>{
  const value={...current};
  for(const [key,old] of Object.entries(value)) { const input=document.querySelector(`[name="${key}"]`); value[key]=input.type==='checkbox'?input.checked:typeof old==='number'?Number(input.value):input.value; }
  await api('/api/preferences','PUT',value); message.textContent='偏好已保存。简报修改后需要重新检查预览。'; await load();
});};
run(load);
