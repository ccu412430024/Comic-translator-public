/* Connection metadata only. OAuth credentials remain in the local service. */
(()=>{
  function ready(settings){return settings?.gemini_ready??settings?.has_gemini_key??false;}
  function payload(settings){
    if(!settings?.gemini_backend)return {};
    return {gemini_backend:settings.gemini_backend,vertex_project:settings.vertex_project||"",vertex_location:settings.vertex_location||"global"};
  }
  function describe(settings){
    if(settings?.gemini_backend!=="vertex")return "Google AI Studio · API 金鑰";
    return "Vertex AI · "+(settings.vertex_project||"尚未設定專案")+" · "+(settings.vertex_location||"global");
  }
  function problem(settings){
    if(settings?.gemini_backend!=="vertex")return "請先在本機設定頁填入 Gemini API 金鑰。";
    if(!settings.vertex_project)return "請先在本機設定頁填入 GCP 專案 ID。";
    return settings.vertex_auth?.message||"請先完成 Google Cloud 本機登入，並測試 Vertex AI 連線。";
  }
  function priorityNote(settings){
    return settings?.gemini_backend==="vertex"?"Vertex AI 使用 Priority PayGo，費率與可用性依模型及專案；不可用時改用 Standard。":"AI Studio 的 Priority 需要 Tier 2／3，費用較高；不可用時改用 Standard。";
  }
  globalThis.ComicGeminiConnection={ready,payload,describe,problem,priorityNote};
})();
