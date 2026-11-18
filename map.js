
let cityMap, cityPosition;
const cityCenter = [54.632, 39.746];
const pinIcons = {culture:"♪",nature:"♧",food:"☕",fun:"✦",active:"⚑",unusual:"◇"};
const tileSize = 256;
function cityMapView(){
  return `<section class="city-board">
    <div class="city-heading"><h1>Рязань</h1><div class="city-filters" aria-label="Категории">
    <button type="button" data-city-filter="" aria-pressed="true">Все места</button>
    ${Object.entries(cats).map(([key,label])=>`<button type="button" data-city-filter="${key}" aria-pressed="false">${label}</button>`).join("")}
    </div></div>
    <div class="city-grid">
      <section class="city-viewport" aria-label="Интерактивная карта">
        <div class="city-map" id="city-map" tabindex="0" aria-label="Карта. Стрелки перемещают, плюс и минус меняют масштаб.">
          <div id="city-tiles" class="city-tiles" aria-hidden="true"></div>
          <div id="city-pins" class="city-pins"></div>
        </div>
        <p id="city-status" class="city-status" role="status" hidden></p>
        <div class="city-actions"><button id="city-locate" type="button" aria-label="Моё местоположение" title="Моё местоположение">◎</button><button id="city-reset" type="button" aria-label="Вернуться в Рязань" title="Вернуться в Рязань">⌂</button></div>
        <div class="city-zoom"><button id="city-plus" aria-label="Приблизить">+</button><button id="city-minus" aria-label="Отдалить">−</button></div>
        <a class="city-attribution" href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a>
      </section>

    </div>
  </section>`;
}
function project([lat,lon], zoom){
  const size=tileSize*2**zoom;
  const sine=Math.sin(Math.max(-85,Math.min(85,lat))*Math.PI/180);
  return [(lon+180)/360*size,(.5-Math.log((1+sine)/(1-sine))/(4*Math.PI))*size];
}
function unproject([x,y], zoom){
  const size=tileSize*2**zoom;
  return [Math.atan(Math.sinh(Math.PI*(1-2*y/size)))*180/Math.PI,x/size*360-180];
}
function clampCity(center){
  return [Math.max(54.05,Math.min(55.20,center[0])),Math.max(38.8,Math.min(40.7,center[1]))];
}
function position(){
  return new Promise((resolve,reject)=>{
    if(!navigator.geolocation)return reject(Error("Геолокация недоступна в этом браузере."));
    navigator.geolocation.getCurrentPosition(p=>resolve([p.coords.latitude,p.coords.longitude]),
      ()=>reject(Error("Не удалось получить геолокацию. Проверьте разрешение браузера.")),{timeout:10000,maximumAge:10000});
  });
}
function closeCityMap(){
  if(!cityMap)return;
  clearInterval(cityMap.poll);
  cancelAnimationFrame(cityMap.frame);
  cityMap.resize.disconnect();
  cityMap=null;
}
function scheduleCity(context){
  if(cityMap!==context||context.frame)return;
  context.frame=requestAnimationFrame(()=>{
    context.frame=0;
    if(cityMap===context)drawCity(context);
  });
}
function drawCity(context){
  const {canvas,zoom,tiles,pins}=context;
  const width=canvas.clientWidth,height=canvas.clientHeight;
  if(!width||!height)return;
  const [x,y]=project(context.center,zoom);
  const left=x-width/2,top=y-height/2;
  const visible=new Set();
  const tileZoom=Math.floor(zoom), span=tileSize*2**(zoom-tileZoom);
  for(let column=Math.floor(left/span);column<=Math.floor((left+width)/span);column++){
    for(let row=Math.floor(top/span);row<=Math.floor((top+height)/span);row++){
      const key=tileZoom+"/"+column+"/"+row;
      let image=context.images.get(key);
      if(!image){
        image=document.createElement("img");
        image.alt="";image.draggable=false;
        image.src="https://tile.openstreetmap.org/"+key+".png";
        image.onerror=()=>{
          if(cityMap!==context||!image.isConnected)return;
          context.status.textContent="Часть подложки карты недоступна";
          context.status.hidden=false;
        };
        image.onload=()=>{
          if(cityMap===context&&image.isConnected&&!Array.from(context.images.values()).some(tile=>tile.complete&&!tile.naturalWidth)){
            context.status.hidden=true;
          }
        };
        context.images.set(key,image);
        tiles.append(image);
      }
      visible.add(key);
      image.style.transform=`translate3d(${column*span-left}px,${row*span-top}px,0) scale(${span/tileSize})`;
    }
  }
  for(const [key,image] of context.images){
    if(!visible.has(key)){image.remove();context.images.delete(key);}
  }

  const offset=point=>{
    const [px,py]=project(point,zoom);
    return [px-left,py-top];
  };
  const inside=([px,py])=>px>=-24&&px<=width+24&&py>=-24&&py<=height+24;
  const existingFocus=document.activeElement?.dataset?.pin;
  const markers=[];
  function marker(point, label, text, style, key, click){
    if(!inside(point))return;
    const element=document.createElement(click?"button":"span");
    if(click){element.type="button";element.onclick=click;}
    element.className="board-pin "+style;
    element.dataset.pin=key;
    element.title=label;
    element.setAttribute("aria-label",label);
    element.textContent=text;
    element.style.left=point[0]+"px";element.style.top=point[1]+"px";
    markers.push(element);
  }
  const groups=new Map();
  for(const place of context.places){
    if(context.category&&place.category!==context.category)continue;
    const point=offset([place.lat,place.lon]);
    if(!inside(point))continue;
    const key=Math.floor(point[0]/48)+":"+Math.floor(point[1]/48);
    if(!groups.has(key))groups.set(key,[]);
    groups.get(key).push({point,place});
  }
  for(const items of groups.values()){
    const {place,point}=items[0];
    if(items.length===1){
      marker(point,place.name,pinIcons[place.category]||"◇","map-place map-"+place.category,place.id,()=>details(place));
    }else{
      marker(point,`${items.length} мест`,String(items.length),"map-cluster","group-"+place.id,()=>{
        if(zoom<16){context.center=[place.lat,place.lon];changeZoom(context,2);return;}
        openDialog(`<section class="stack"><h2>Места рядом</h2>${items.map(({place:p})=>`<button data-map-place="${esc(p.id)}">${esc(p.name)}</button>`).join("")}</section>`);
      });
    }
  }
  if(cityPosition)marker(offset(cityPosition),"Вы","Вы","map-self","self");
  for(const friend of context.social?.people||[]){
    if(!friend.location)continue;
    const name=friend.nickname?"@"+friend.nickname:friend.name;
    marker(offset(friend.location),name+" · примерная позиция",friend.name.slice(0,1).toUpperCase(),
      "map-friend",friend.code,()=>openDialog(`<section class="stack"><h2>${esc(friend.name)}</h2><p>${esc(name)} · примерная геопозиция</p></section>`));
  }
  pins.replaceChildren(...markers);
  if(existingFocus)markers.find(marker=>marker.dataset.pin===existingFocus)?.focus({preventScroll:true});
}
function zoomAt(context, zoom, before, after=before){
  const next=Math.max(11,Math.min(18,zoom));
  const anchor=unproject(project(context.center,context.zoom).map((v,i)=>v+before[i]),context.zoom);
  context.center=clampCity(unproject(project(anchor,next).map((v,i)=>v-after[i]),next));
  context.zoom=next;
  scheduleCity(context);
}
function changeZoom(context,delta){
  zoomAt(context,context.zoom+delta,[0,0]);
}
function bindMapGestures(context){
  const {canvas}=context;
  const pointers=new Map();
  let start, lastTap, multi=false;
  const relative=point=>{
    const rect=canvas.getBoundingClientRect();
    return [point[0]-rect.left-rect.width/2,point[1]-rect.top-rect.height/2];
  };
  const midpoint=(a,b)=>[(a[0]+b[0])/2,(a[1]+b[1])/2];
  const gap=(a,b)=>Math.hypot(a[0]-b[0],a[1]-b[1]);
  canvas.onpointerdown=event=>{
    if(event.target.closest("button")||event.button!==0&&event.pointerType==="mouse"||pointers.size>=2)return;
    const point=[event.clientX,event.clientY];
    if(!pointers.size){start=point;multi=false;}
    pointers.set(event.pointerId,point);
    if(pointers.size===2)multi=true;
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("dragging");
  };
  canvas.onpointermove=event=>{
    const previous=pointers.get(event.pointerId);
    if(!previous)return;
    const current=[event.clientX,event.clientY];
    pointers.set(event.pointerId,current);
    if(pointers.size===2){
      const other=[...pointers.entries()].find(([id])=>id!==event.pointerId)[1];
      const oldDistance=gap(other,previous),newDistance=gap(other,current);
      if(oldDistance>3&&newDistance>3){
        zoomAt(context,context.zoom+Math.log2(newDistance/oldDistance),
          relative(midpoint(other,previous)),relative(midpoint(other,current)));
      }
    }else{
      const [x,y]=project(context.center,context.zoom);
      context.center=clampCity(unproject([x-current[0]+previous[0],y-current[1]+previous[1]],context.zoom));
      scheduleCity(context);
    }
  };
  canvas.onpointerup=event=>{
    if(!pointers.has(event.pointerId)||event.pointerType==="mouse"||multi)return;
    const point=[event.clientX,event.clientY],time=performance.now();
    if(gap(point,start)>12){lastTap=null;return;}
    if(lastTap&&time-lastTap.time<330&&gap(point,lastTap.point)<25){
      zoomAt(context,context.zoom+1,relative(point));lastTap=null;
    }else lastTap={time,point};
  };
  canvas.ondblclick=event=>{
    if(event.target.closest("button")||event.sourceCapabilities?.firesTouchEvents)return;
    zoomAt(context,context.zoom+1,relative([event.clientX,event.clientY]));
  };
  canvas.onlostpointercapture=event=>{
    pointers.delete(event.pointerId);
    if(!pointers.size)canvas.classList.remove("dragging");
  };
  canvas.addEventListener("wheel",event=>{
    event.preventDefault();
    const now=performance.now();
    if(now-(context.lastWheel||0)<160)return;
    context.lastWheel=now;
    changeZoom(context,event.deltaY>0?-1:1);
  },{passive:false});
  canvas.addEventListener("keydown",event=>{
    if(event.target!==canvas)return;
    const movement={ArrowLeft:[-80,0],ArrowRight:[80,0],ArrowUp:[0,-80],ArrowDown:[0,80]}[event.key];
    if(movement){
      event.preventDefault();
      const [x,y]=project(context.center,context.zoom);
      context.center=clampCity(unproject([x+movement[0],y+movement[1]],context.zoom));
      scheduleCity(context);
    }else if(["+","=","-"].includes(event.key)){
      event.preventDefault();changeZoom(context,event.key==="-"?-1:1);
    }
  });
}
function paintCityPeople(context){
  const social=context.social;
  const friends=social.people.filter(p=>p.status==="friend");
  context.count.textContent=friends.length?String(friends.length):"";
  context.people.innerHTML=social.people.map(p=>`<article class="city-person">
    <span class="city-avatar">${esc(p.name.slice(0,1).toUpperCase())}</span>
    <div><strong>${esc(p.name)}</strong>${p.nickname?` <span class="hint">@${esc(p.nickname)}</span>`:""}<p class="hint">${p.status==="incoming"?"Входящая заявка":p.status==="outgoing"?"Заявка отправлена":p.location?"Делится геопозицией":"Геопозиция скрыта"}</p></div>
    <div class="city-person-actions">${p.status==="incoming"?`<button data-city-person="accept" data-code="${esc(p.code)}">Принять</button>`:""}
    ${p.location?`<button data-city-person="focus" data-code="${esc(p.code)}">На карте</button>`:""}
    <button data-city-person="remove" data-code="${esc(p.code)}">${p.status==="friend"?"Удалить":p.status==="incoming"?"Отклонить":"Отменить"}</button></div>
    </article>`).join("");
  if(!social.people.length)context.people.textContent="Здесь появятся заявки и друзья.";
  context.share.disabled=false;
  context.share.textContent=social.sharingUntil?"Прекратить делиться":"Поделиться на 15 минут";
  context.sharingStatus.textContent=social.sharingUntil
    ?`Друзья видят примерную позицию до ${new Date(social.sharingUntil*1000).toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})}.`
    :"Примерная позиция видна подтверждённым друзьям в течение 15 минут.";
  scheduleCity(context);
}
async function refreshCityPeople(context){
  const social=await api("friends");
  if(cityMap!==context)return;
  if(JSON.stringify(social)!==JSON.stringify(context.social)){
    context.social=social;
    paintCityPeople(context);
  }
}
function mountCityMap(token){
  const get=id=>document.getElementById("city-"+id);
  const context={category:"",center:cityCenter,zoom:13,places:[],images:new Map(),canvas:get("map"),tiles:get("tiles"),pins:get("pins"),
    status:get("status"),count:get("count"),people:get("people"),share:get("share"),sharingStatus:get("sharing-status")};
  cityMap=context;
  async function work(fn){
    try{await fn();}
    catch(error){if(cityMap===context)toast(error.message);}
  }
  const form=get("friend-form");
  if(form)form.onsubmit=event=>{
    event.preventDefault();
    const input=get("add");
    work(async()=>{
      await api("friends",{action:"request",nickname:input.value});
      input.value="";
      await refreshCityPeople(context);
      if(cityMap===context)toast("Заявка отправлена");
    });
  };
  if(context.people)context.people.onclick=event=>{
    const {cityPerson:action,code}=event.target.dataset;
    if(!action)return;
    const friend=context.social.people.find(p=>p.code===code);
    if(action==="focus"){
      context.center=clampCity(friend.location);context.zoom=15;scheduleCity(context);return;
    }
    work(async()=>{await api("friends",{action,code});await refreshCityPeople(context);});
  };
  get("locate").onclick=()=>work(async()=>{
    const coordinates=await position();
    if(cityMap!==context)return;
    const center=clampCity(coordinates);
    if(center.some((value,index)=>value!==coordinates[index]))throw Error("Вы за пределами карты Рязани и окрестностей.");
    cityPosition=coordinates;context.center=center;context.zoom=15;scheduleCity(context);
  });
  if(context.share)context.share.onclick=()=>work(async()=>{
    context.share.disabled=true;
    try{
      if(context.social.sharingUntil)await api("location",{},"DELETE");
      else{
        const coordinate=await position();
        if(cityMap!==context)return;
        cityPosition=coordinate;
        await api("location",{lat:Number(coordinate[0].toFixed(2)),lon:Number(coordinate[1].toFixed(2))});
        scheduleCity(context);
      }
      await refreshCityPeople(context);
    }finally{context.share.disabled=false;}
  });
  get("reset").onclick=()=>{context.center=cityCenter;context.zoom=13;scheduleCity(context);};
  get("plus").onclick=()=>changeZoom(context,1);
  get("minus").onclick=()=>changeZoom(context,-1);
  for(const filter of main.querySelectorAll("[data-city-filter]")){
    filter.onclick=()=>{
      context.category=filter.dataset.cityFilter;
      for(const sibling of main.querySelectorAll("[data-city-filter]"))sibling.setAttribute("aria-pressed",String(sibling===filter));
      scheduleCity(context);
    };
  }
  bindMapGestures(context);
  context.resize=new ResizeObserver(()=>scheduleCity(context));
  context.resize.observe(context.canvas);
  work(async()=>{
    const places=await api("map");
    if(cityMap!==context||token!==version)return;
    context.places=places;scheduleCity(context);
  });
}
document.addEventListener("click",event=>{
  const id=event.target.closest("[data-map-place]")?.dataset.mapPlace;
  if(id&&cityMap)details(cityMap.places.find(place=>place.id===id));
});

