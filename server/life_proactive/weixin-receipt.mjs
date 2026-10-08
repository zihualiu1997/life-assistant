export function classifyReceipt(result, clientId) {
  if (!result || Array.isArray(result) || typeof result !== 'object')
    return {status:'unknown',error:'wechat_invalid_receipt'};
  for (const key of ['ret','errcode']) {
    if (result[key] !== undefined && result[key] !== 0)
      return {status:typeof result[key]==='number'?'failed':'unknown',error:'wechat_rejected_or_invalid_receipt'};
  }
  return {status:'sent',message_id:String(result.message_id || clientId),
    receipt_fields:Object.keys(result).filter(k=>['ret','errcode','message_id'].includes(k)),
    meaning:'provider_accepted_not_read_receipt'};
}
