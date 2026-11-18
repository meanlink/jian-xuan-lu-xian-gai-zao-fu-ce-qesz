/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{team:string,available:boolean,job_id:?number,job_name:?string,mean_saved_seconds:?number,hours_per_pick:?number,basis:?string}} RateCard */
/** @typedef {{id:number,month:string,closed:boolean,closed_at:?number,closed_by:?string,entry_count:number,volume:number,saved_hours:number,postings:Array}} AccountingPeriodView */
/** @typedef {{id:number,period_id:number,entry_id:?number,team:string,shift_date:string,source:string,volume:number,rate_snapshot:Object,saved_hours:number,adjustment:Object}} Posting */
export {};
