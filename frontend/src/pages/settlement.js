import {settlement} from '../api/entities.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {heading} from '../components/common/shell.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const SOURCE_LABEL={initial:'初始入账',correction:'更正补差',late:'迟到单量'};
const canWrite=()=>session.user.role!=='viewer';
const isAdmin=()=>session.user.role==='admin';

export async function settlementPage(){
 const [card,periods]=await Promise.all([settlement.rateCard(),settlement.periods()]);
 const selected=periods.find(p=>p.id===workspace.selectedPeriodId)||periods.find(p=>!p.closed)||periods[0]||null;
 return heading('06 / MONTH-END CLOSE','月末节省工时结算','按班组已批准复测均值折算每单节省工时，结合实拣单量估算节省工时。同班一次入账并锁定结论与折算值；关账后原数保留，迟到单量与更正差额结转到首个未关账月份。','<button class="button" id="refresh-settlement">刷新结算台 ↻</button>')
 +'<section class="panel"><div class="section-title"><h2>折算值卡 · 已批准复测结论锁定</h2><span>节省工时 = 实拣单量 × 平均节省秒数 ÷ 3600</span></div><div class="rate-grid">'
 +card.map(c=>'<article class="rate-card '+(!c.available?'unavailable':'')+'"><strong>'+esc(c.team)+'</strong>'+(c.available
   ?'<span>'+esc(c.basis)+' · '+esc(c.job_name)+'</span><small>平均节省 '+fmt(c.mean_saved_seconds)+' 秒/单</small><small class="rate-value">'+fmt(c.hours_per_pick)+' 工时/单</small>'
   :'<span class="missing">尚无已批准复测结论</span><small>登记前需先在结果复核中批准</small>')+'</article>').join('')
 +'</div></section>'
 +'<div class="settlement-grid"><section class="panel"><div class="section-title"><h2>登记班次单量</h2><span>同一班次只入账一次</span></div>'
 +'<form id="volume-form"><label>所属月份（YYYY-MM，留空取班次日期所在月）<input name="month" placeholder="2026-10" maxlength="7"></label><div class="parameter-grid"><label>班组<select name="team">'+card.map(c=>'<option value="'+esc(c.team)+'" '+(c.available?'':'disabled')+'>'+esc(c.team)+(c.available?'':'（无可用结论）')+'</option>').join('')+'</select></label><label>班次日期<input name="shift_date" type="date" required></label></div><div class="parameter-grid"><label>实拣单量（单）<input name="volume" type="number" min="0" step="1" required placeholder="1200"></label><label>迟到单量<select name="late"><option value="false">当月正常登记</option><option value="true">迟到单量（计入首个未关账月份）</option></select></label></div><label>备注<input name="note" maxlength="300" placeholder="选填，如：仓储系统延迟推送"></label><button type="submit" class="primary" '+(canWrite()?'':'disabled')+'>登记并锁定折算值 ↗</button><p class="hint">登记瞬间快照所用复测结论与折算值；观察员只读。网络重试由幂等键保护，不会重复入账。</p></form></section>'
 +'<section class="panel"><div class="section-title"><h2>会计期间与关账</h2><span>关账由业务负责人执行</span></div><div class="period-list">'+(periods.length?periods.map(p=>'<article class="period-item'+(p.id===selected?.id?' selected':'')+'" data-period="'+p.id+'"><div><strong>'+esc(p.month)+(p.closed?' · 已关账':' · 未关账')+'</strong><p>'+p.entry_count+' 个班次 · 入账 '+fmt(p.volume)+' 单 · 节省 '+fmt(p.saved_hours)+' 工时</p>'+(p.closed?'<small>关账于 '+(p.closed_at?date(p.closed_at):'—')+' · '+esc(p.closed_by||'')+'</small>':'<small>未关账：单量可更正，原分录按锁定折算值重算</small>')+'</div>'+(isAdmin()&&!p.closed?'<button class="button small" data-close="'+esc(p.month)+'">执行关账</button>':'')+'</article>').join(''):'<p class="empty">尚无会计期间，登记第一笔班次单量时自动建立。</p>')+'</div></section></div>'
 +'<section class="panel"><div class="section-title"><h2>入账与补差核对 · '+(selected?esc(selected.month):'')+'</h2><span>原数与补差逐笔可溯</span></div>'+postingTable(selected)+'</section>';
}

function postingTable(period){
 if(!period)return '<div class="empty">选择一个期间查看分录。</div>';
 const rows=period.postings||[];
 if(!rows.length)return '<div class="empty">该期间暂无入账分录。</div>';
 const cols=[['shift_date','班次日期'],['team','班组'],['source','分录类型'],['volume','入账单量'],['saved_hours','节省工时'],['rate','折算值（工时/单）'],['basis','结论依据'],['adjustment','补差说明']];
 const correctable=rows.filter(p=>p.entry_id&&p.entry&&p.entry.period_id===period.id);
 return '<div class="table-wrap"><table><thead><tr>'+cols.map(c=>'<th>'+esc(c[1])+'</th>').join('')+'</tr></thead><tbody>'
 +rows.map(p=>'<tr><td>'+esc(p.shift_date)+'</td><td>'+esc(p.team)+'</td><td><span class="tag tag-'+esc(p.source)+'">'+esc(SOURCE_LABEL[p.source]||p.source)+'</span></td><td>'+fmt(p.volume)+'</td><td>'+fmt(p.saved_hours)+'</td><td>'+fmt(p.rate_snapshot?.hours_per_pick)+'</td><td>'+esc(p.rate_snapshot?.basis||'')+' <small>#'+esc(p.rate_snapshot?.job_id??'')+'</small></td><td>'+adjustmentText(p)+'</td></tr>').join('')
 +'</tbody></table></div>'
 +(period.closed?'':(correctable.length?'<div class="correct-zone"><h3>登记更正（未关账期间）</h3><form id="correct-form"><div class="parameter-grid"><label>选择班次分录<select name="posting_id">'+correctable.map(p=>'<option value="'+p.entry_id+'">'+esc(p.shift_date)+' · '+esc(p.team)+' · 当前 '+fmt(p.entry.volume)+' 单</option>').join('')+'</select></label><label>更正后单量<input name="new_volume" type="number" min="0" step="1" required></label></div><label>更正说明<input name="note" maxlength="300" placeholder="如：盘点复核补记"></label><button type="submit" class="primary" '+(canWrite()?'':'disabled')+'>提交更正 ↗</button><p class="hint">未关账：原分录按原折算值就地重算；若所选班次所属期间已关账，差额将自动结转到首个未关账月份并沿用原折算值。已结转的补差分录不在此重复更正。</p></form></div>':'<div class="correct-zone"><h3>登记更正（未关账期间）</h3><p class="empty">当前期间只有承接的迟到单量或关账补差，没有可就地更正的本班次登记。</p></div>'));
}

function adjustmentText(p){
 const a=p.adjustment||{};
 if(p.source==='correction'&&a.origin_month){
  return esc(a.origin_month)+' 关账差额 '+fmt(a.delta_volume)+' 单（'+fmt(a.delta_hours)+' 工时）'+(a.note?' · '+esc(a.note):'');
 }
 if(p.source==='correction'){
  return '原数 '+fmt(a.old_volume)+' → '+fmt(a.new_volume)+'，差 '+fmt(a.delta_volume)+' 单'+(a.note?' · '+esc(a.note):'');
 }
 if(p.source==='late')return '迟到单量，按锁定折算值入账';
 return '—';
}

export function bindSettlement(refresh){
 document.querySelector('#refresh-settlement').onclick=refresh;
 submit(document.querySelector('#volume-form'),async f=>{
  const month=(f.get('month')||'').trim();
  await settlement.register({team:f.get('team'),shift_date:f.get('shift_date'),volume:Number(f.get('volume')),
   late:f.get('late')==='true',note:f.get('note')||'',...(month?{month}:{})});
  notify('班次单量已登记，折算值与结论已快照锁定');await refresh();
 });
 document.querySelectorAll('[data-period]').forEach(el=>el.addEventListener('click',()=>{
  workspace.selectedPeriodId=Number(el.dataset.period);refresh();
 }));
 document.querySelectorAll('[data-close]').forEach(btn=>btn.addEventListener('click',async(e)=>{
  e.stopPropagation();
  const month=btn.dataset.close;
  if(!confirm('确认关账 '+month+'？关账后原数保留，后续更正与迟到单量只能结转到首个未关账月份。'))return;
  try{const r=await settlement.close(month);notify(r.closed_now?('月份 '+month+' 已关账，累计节省 '+r.saved_hours+' 工时'):'该月份已处于关账状态，未重复处理');await refresh();}
  catch(e){notify(e.message,true);}
 }));
 const correctForm=document.querySelector('#correct-form');
 if(correctForm)submit(correctForm,async f=>{
  const r=await settlement.correct(Number(f.get('posting_id')),Number(f.get('new_volume')),f.get('note')||'');
  notify(r.source==='correction'&&r.adjustment?.origin_month?'已关账：原数保留，差额 '+r.adjustment.delta_volume+' 单已结转到首个未关账月份':'未关账期间已按原折算值重算入账');
  await refresh();
 });
}
