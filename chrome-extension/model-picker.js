"use strict";
// Shared by the popup and local reader; no credentials are stored here.
globalThis.ComicGeminiModels = (()=>{
  const valid=value=>typeof value==="string"&&/^[a-zA-Z0-9_.-]{1,100}$/.test(value);
  function create({select,input,field,onChange=()=>{}}){
    let ids=new Set();
    const value=()=>select.value==="custom"?input.value.trim():select.value;
    function visibility(){field.hidden=select.value!=="custom";}
    function set(model){
      if(!valid(model))return false;
      select.value=ids.has(model)?model:"custom";
      if(select.value==="custom")input.value=model;
      visibility();return true;
    }
    function load(models,current){
      select.replaceChildren();ids=new Set();
      for(const model of models||[]){
        if(!valid(model.id)||ids.has(model.id))continue;
        const option=document.createElement("option");option.value=model.id;option.textContent=model.name||model.id;
        select.append(option);ids.add(model.id);
      }
      const custom=document.createElement("option");custom.value="custom";custom.textContent="自訂模型名稱…";select.append(custom);
      set(valid(current)?current:ids.values().next().value||"gemini-3.5-flash-lite");
    }
    select.addEventListener("change",()=>{visibility();onChange(value());});
    input.addEventListener("input",()=>onChange(value()));
    return {value,set,load,valid:()=>valid(value())};
  }
  return {valid,create};
})();
