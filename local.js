
const storageKey = "swipecity-pages-v1:"+location.pathname;
let localHistory = {};
try { localHistory = JSON.parse(localStorage.getItem(storageKey) || "{}") || {}; }
catch { localHistory = {}; }
let catalogRequest;

function getCatalog(){
  if(!catalogRequest) catalogRequest=fetch("./catalog.json",{cache:"no-cache"}).then(response=>{
    if(!response.ok) throw Error("Каталог временно недоступен");
    return response.json();
  }).catch(error=>{catalogRequest=null;throw error;});
  return catalogRequest;
}
function keepHistory(){
  try { localStorage.setItem(storageKey,JSON.stringify(localHistory)); }
  catch { toast("Браузер не разрешил постоянное сохранение."); }
}
function kmAway(origin,place){
  const rad=Math.PI/180;
  const a=Math.sin((place.lat-origin[0])*rad/2)**2+
    Math.cos(place.lat*rad)*Math.cos(origin[0]*rad)*Math.sin((place.lon-origin[1])*rad/2)**2;
  return 12742*Math.asin(Math.sqrt(Math.min(1,a)));
}
async function api(path,data={},method="GET"){
  if(path==="profile") return {name:"Гость",nickname:null,interests:[]};
  const places=await getCatalog();
  if(path==="map")return places.filter(p=>p.verified);
  if(path==="feed"||path==="counts"){
    const origin=data.origin||[54.632,39.746];
    const available=places.filter(p=>p.verified&&!Object.hasOwn(localHistory,p.id)&&
      (data.budget==null||(p.price==null?data.includeUnknown:p.price<=data.budget)))
      .map(p=>({...p,km:Math.round(kmAway(origin,p)*100)/100}))
      .filter(p=>p.km<=(data.radius||5));
    if(path==="counts")return available.reduce((counts,p)=>{
      counts[p.category]=(counts[p.category]||0)+1;
      return counts;
    },{});
    return available.filter(p=>!data.categories?.length||data.categories.includes(p.category))
      .sort((a,b)=>Number(Boolean(b.photo))-Number(Boolean(a.photo))||a.km-b.km||a.id.localeCompare(b.id));
  }
  if(path==="library")return places.filter(p=>Object.hasOwn(localHistory,p.id)&&localHistory[p.id].action!=="skip")
    .map(p=>({...localHistory[p.id],placeData:p})).sort((a,b)=>b.created.localeCompare(a.created));
  if(path==="undo"){
    const latest=Object.entries(localHistory).sort((a,b)=>b[1].created.localeCompare(a[1].created))[0];
    if(latest)delete localHistory[latest[0]];
  }else{
    if(!places.some(p=>p.id===data.place))throw Error("Место не найдено");
    if(path==="swipe"&&["like","skip","visit"].includes(data.action)){
      localHistory[data.place]={action:data.action,created:new Date().toISOString(),rating:null,review:""};
    }else if(path==="saved"&&localHistory[data.place]?.action==="like"){
      localHistory[data.place].action="skip";
    }else if(path==="visit"&&localHistory[data.place]?.action==="visit"){
      Object.assign(localHistory[data.place],{rating:data.rating,review:data.review});
    }else throw Error("Действие недоступно");
  }
  keepHistory();
  return {};
}

