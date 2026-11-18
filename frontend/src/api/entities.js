import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
const idem=()=> (crypto.randomUUID?crypto.randomUUID():('idem-'+Date.now()+'-'+Math.random().toString(16).slice(2)));
export const settlement={
 rateCard:()=>api('/settlement/rate-card'),
 periods:()=>api('/settlement/periods'),
 entries:periodId=>api('/settlement/entries'+(periodId?('?period_id='+periodId):'')),
 register:data=>api('/settlement/volumes',{method:'POST',body:JSON.stringify({idempotency_key:idem(),...data})}),
 correct:(entryId,newVolume,note)=>api('/settlement/entries/'+entryId+'/correction',{method:'POST',body:JSON.stringify({new_volume:newVolume,note:note||'',idempotency_key:idem()})}),
 close:month=>api('/settlement/close',{method:'POST',body:JSON.stringify({month})})
};
