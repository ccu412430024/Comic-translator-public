/* Shared display of observed Gemini metrics; never infer an unreported tier. */
(()=>{
  function format(metrics){
    const a=metrics?.gemini_acceleration||metrics;
    if(!a?.cache_state)return "";
    if(a.cache_state==="local_cache")return "本機譯文快取 · 未呼叫 Gemini";
    if(a.cache_state==="not_used")return "";
    const states={created:"雲端快取已建立",reused:"重用雲端快取",too_small:"固定前綴未達快取門檻，使用一般翻譯",
      unsupported:"模型不支援顯式快取，使用一般翻譯",unavailable:"雲端快取暫不可用，使用一般翻譯",
      cleanup_pending:"雲端快取待清理，使用一般翻譯",expired_fallback:"雲端快取失效，已改一般翻譯",no_prefix:"沒有可快取的固定前綴"};
    const parts=[];
    if(a.backend==="vertex")parts.push("Vertex AI");
    if(states[a.cache_state])parts.push(states[a.cache_state]);
    if(a.cache_state==="reused"&&a.cache_renewal==="renewed")parts.push("快取已續期");
    if(a.cache_state==="reused"&&a.cache_renewal==="failed")parts.push("續期失敗，沿用原到期時間");
    if(a.context_policy==="cached_fixed")parts.push("固定資料獨立額度");
    else if(a.context_policy==="combined"&&a.cache_mode==="explicit")parts.push("已按合計上限裁切");
    if(a.cache_state==="too_small"&&Number.isFinite(a.prefix_tokens))parts.push(`前綴 ${a.prefix_tokens}${Number.isFinite(a.minimum_tokens)?"／最低 "+a.minimum_tokens:""} token`);
    if(Number.isFinite(a.cached_tokens))parts.push(`${a.cache_mode==="off"?"隱式快取":"快取"}命中 ${a.cached_tokens} token`);
    if(a.actual_tier==="priority")parts.push("Priority");
    else if(a.actual_tier==="standard")parts.push(a.requested_tier==="priority"?"已改用 Standard":"Standard");
    else if(a.actual_tier==="unknown")parts.push(a.submitted_tier==="priority"?"已請求 Priority，服務等級未回報":a.priority_fallback?"Priority 不可用，已請求 Standard":"服務等級未回報");
    if(Number.isFinite(a.seconds))parts.push(`Gemini ${a.seconds.toFixed(2)} 秒`);
    if(Number.isInteger(a.retry_429)&&a.retry_429>0)parts.push(`429 重試 ${a.retry_429} 次`);
    const retry503=a.retry_503??(a.retry_429===undefined?a.retry_count:0);
    if(Number.isInteger(retry503)&&retry503>0)parts.push(`503 重試 ${retry503} 次`);
    if(a.retry_wait_seconds>=1)parts.push(`冷卻 ${a.retry_wait_seconds.toFixed(1)} 秒`);
    return parts.join(" · ");
  }
  globalThis.ComicGeminiStatus={format};
})();
