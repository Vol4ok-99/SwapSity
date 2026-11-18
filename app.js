
const main = document.querySelector("main"), dialog = document.querySelector("dialog");
const cats = {culture:"Культура",nature:"Прогулки",food:"Еда и напитки",fun:"Развлечения",active:"Активный отдых",unusual:"Необычные места"};
let page, room, state, current, me, busy, version=0, poll, timer;
let filters={radius:5,categories:[],budget:null};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const link=s=>typeof s==="string"&&s.startsWith("https://")?esc(s):"#";
const intro=(label,title,description)=>`<section class="intro"><p class="kicker">${label}</p><h1>${title}</h1><p>${description}</p></section>`;
const empty=(title,message)=>`<div class="panel empty"><h2>${title}</h2><p class="muted">${message}</p></div>`;
const btn=(action,label,id="",cls="")=>`<button type="button" class="${cls}" data-action="${action}" data-id="${esc(id)}">${label}</button>`;
function toast(message){
  const notice=document.querySelector("#notice");
  notice.textContent=message; notice.hidden=false;
  clearTimeout(timer); timer=setTimeout(()=>notice.hidden=true,4200);
}
function photo(p){
  const source=p.photo?.url;
  const href=typeof source==="string"&&/^\/media\/[a-f0-9]{64}\.(jpg|png|webp)$/.test(source)?source:link(source);
  return p.photo?`<img src="${href}" alt="${esc(p.name)}" referrerpolicy="no-referrer" draggable="false">`:"";
}
function credit(p){
  if(p.photo?.local)return `<span class="photo-credit">Фото: ${esc(p.photo.author)}</span>`;
  return p.photo?`<span class="photo-credit"><a href="${link(p.photo.source)}" target="_blank" rel="noopener">${esc(p.photo.attribution||p.photo.author)}</a> · <a href="${link(p.photo.license)}" target="_blank" rel="noopener">${esc(p.photo.license_label||"CC BY-SA 4.0")}</a></span>`:`<span class="no-photo">Фотографии пока нет</span>`;
}
function card(p){
  current=p;
  return `<article class="card immersive-card" data-cat="${esc(p.category)}">
    <div class="card-bg"></div>${photo(p)}
    <header class="card-top"><span class="feed-city">Рязань</span>${btn("details","Инфо ↗",p.id,"details")}</header>
    <div class="place-intro">
      <p class="place-kind">${esc(p.kind||cats[p.category])} <span>· ${p.km} км</span></p>
      <h1>${esc(p.name)}</h1>
      <p class="place-preview">${esc(p.description)}</p>
      ${credit(p)}
      <div class="swipe-hints" aria-label="Действия с карточкой">
        ${btn("skip","← Дальше")}${btn("visit","↑ Уже был")}${btn("like","Сохранить →")}
      </div>
    </div>
    <span class="swipe-feedback" aria-hidden="true"></span>
  </article>`;
}
function chips(selected){
  return `<div class="chips">${Object.entries(cats).map(([id,label])=>`<label class="chip"><input type="checkbox" name="category" value="${id}" ${selected.includes(id)?"checked":""}>${label}</label>`).join("")}</div>`;
}
function filterFields(f){
  return `<label>Радиус<select name="radius">${[1,3,5,10,25,50].map(n=>`<option value="${n}" ${f.radius===n?"selected":""}>${n} км</option>`).join("")}</select></label>
    <fieldset><legend>Категории</legend>${chips(f.categories)}</fieldset>
    <label>Бюджет до, ₽<input name="budget" type="number" min="0" max="100000" placeholder="Любой" value="${f.budget??""}"></label>`;
}
function readFilters(f){
  return {radius:Number(f.get("radius")),categories:f.getAll("category"),budget:f.get("budget")===""?null:Number(f.get("budget"))};
}
function feed(places){
  return `<section class="discover">
    ${places.length?card(places[0]):empty("Подборка закончилась","Поменяйте фильтры или отмените последний свайп.")}
    <div class="feed-tools">
      <details class="mobile-filters"><summary>Фильтры</summary>
        <form class="stack" data-form="filter">${filterFields(filters)}<button class="positive">Показать места</button>
        ${btn("geo",filters.origin?"От центра города":"От моего положения")}</form>
      </details>
      <span>${places.length} мест</span>${btn("undo","↶ Отменить")}
    </div>
  </section>`;
}
function library(rows){
  const saved=page==="saved";
  const visible=rows.filter(r=>r.action===(saved?"like":"visit"));
  return intro("Мои места",saved?"Хочу сходить":"Я был здесь",saved?"Ваш список идей на потом.":"Посещения и личные впечатления.")
    +(visible.length?`<div class="stack narrow">${visible.map(r=>`<article class="panel row"><h2>${esc(r.placeData.name)}</h2>
    <p class="muted">${esc(r.placeData.address||cats[r.placeData.category])}</p>
    ${saved?"":`<p class="hint">${esc(r.created.slice(0,10))}${r.rating?" · ★ "+r.rating+"/5":""}</p><p>${esc(r.review)}</p>`}
    <div class="spread">${btn("details","Подробнее",r.placeData.id)}${btn(saved?"unsave":"review",saved?"Убрать из списка":"Оценка и отзыв",r.placeData.id)}</div></article>`).join("")}</div>`
    :empty("Здесь пока пусто",saved?"Поставьте лайк месту в ленте.":"Отметьте знакомое место кнопкой «Уже был»."));
}
function group(){
  return intro("Рязань","Вместе интереснее","Пригласите друзей, проголосуйте и посмотрите, где совпали ваши планы.")
    +`<form class="panel stack narrow" data-form="create"><h2>Новая комната</h2><label>Город<input value="Рязань" readonly></label>
    <label>Ваше имя<input name="name" required maxlength="40" value="${esc(me.name)}"></label>${filterFields({radius:5,categories:[]})}
    <label>Количество мест<input name="count" type="number" min="2" max="25" value="5" required></label><button class="positive">Создать комнату</button></form>`;
}
function roomView(r){
  if(!r.joined)return intro("Совместный выбор","Вас пригласили",`В комнате ${r.count} мест для голосования.`)
    +`<form class="panel stack narrow" data-form="join"><label>Ваше имя<input name="name" required maxlength="40" value="${esc(me.name)}"></label><button class="positive">Войти в комнату</button></form>`;
  const invite=location.origin+"/#room/"+room;
  const sidebar=`<aside class="panel stack"><h2>Участники</h2><p class="hint">${r.members.map(m=>esc(m.name)+(m.done?" ✓":" · выбирает")).join("<br>")}</p>
    <div class="room-link"><input id="invite" aria-label="Ссылка-приглашение" value="${esc(invite)}" readonly>${btn("copy","Копировать")}</div></aside>`;
  let body;
  if(r.finished){
    body=r.results.length?`<div class="stack">${r.results.map((item,i)=>`<article class="panel stack ${i?"":"result-top"}">
      <p class="kicker">${i?item.match?"Полное совпадение":"Компромисс":"Рекомендация"}</p><h2>${esc(item.place.name)}</h2>
      <p>${item.match?"Подошло всем участникам.":`Подошло ${item.likes} из ${r.members.length} участников.`} При равенстве голосов выше место, которое ближе к центру.</p>
      ${btn("details","Подробнее",item.place.id)}</article>`).join("")}</div>`:empty("Совпадений нет","Ни одно место не получило положительного голоса.");
  } else body=r.next?card(r.next,true):empty("Ваш выбор сохранён","Результаты появятся, когда минимум два участника завершат голосование.");
  return intro("Рязань",r.finished?"Ваш совместный выбор":"Выбираем вместе",`Карточки ${r.picked} из ${r.count} пройдены.`)
    + `<div class="layout"><section>${body}</section>${sidebar}</div>`;
}
function profile(){
  return intro("Настройки","Профиль","Укажите интересы для личных рекомендаций.")
    +`<div class="stack narrow"><form class="panel stack" data-form="profile"><label>Имя<input name="name" required maxlength="40" value="${esc(me.name)}"></label>
    <fieldset><legend>Интересы</legend>${chips(me.interests)}</fieldset><button class="positive">Сохранить профиль</button>
    <p class="hint">Отзывы доступны в вашей истории.</p>${btn("delete","Удалить мои данные","","danger")}</form>
    ${me.nickname?`<section class="panel stack"><h2>@${esc(me.nickname)}</h2>${btn("logout","Выйти из аккаунта")}</section>`:
      `<form class="panel stack" data-form="account"><h2>Аккаунт SwipeCity</h2>
      <label>Ник<input name="nickname" autocomplete="username" required maxlength="25" placeholder="например, @ryazan"></label>
      <label>Пароль<input name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label>
      <p class="hint">Ник нужен, чтобы друзья могли найти вас. Имя в профиле может совпадать с именами других людей.</p>
      <div class="spread"><button class="positive" value="register">Создать аккаунт</button><button value="login">Войти</button></div></form>`}</div>`;
}
function find(id){
  return !id||id===current?.id?current:Array.isArray(state)?state.find(r=>r.placeData?.id===id)?.placeData:state?.results?.find(r=>r.place.id===id)?.place;
}
function openDialog(html){
  dialog.innerHTML=btn("close","Закрыть","","close")+html;
  if(!dialog.open)dialog.showModal();
}
function details(p){
  if(!p)return;
  const query=encodeURIComponent([p.name,"Рязань",p.address].filter(Boolean).join(", "));
  const facts=[["Адрес",p.address],["Часы работы",p.hours],["Стоимость",p.price==null?null:p.price?`${p.price} ₽`:"Бесплатно"]];
  const photos=p.gallery?.length?p.gallery:p.photo?[p.photo]:[];
  openDialog(`<article class="place-sheet">
    <section class="photo-gallery" aria-label="Фото места">
      ${photos.length?`<div class="photo-track" tabindex="0">${photos.map((image,index)=>`<figure>
        <img src="${link(image.url)}" alt="${esc(image.caption||p.name)}" loading="${index?"lazy":"eager"}" referrerpolicy="no-referrer">
        <figcaption>${credit({photo:image})}</figcaption></figure>`).join("")}</div>`:
      `<div class="gallery-empty">Фотографий пока нет</div>`}
      ${photos.length>1?`<div class="gallery-controls"><button type="button" data-photo-step="-1" aria-label="Предыдущее фото">‹</button>
        <span class="photo-count" aria-live="polite">1 / ${photos.length}</span>
        <button type="button" data-photo-step="1" aria-label="Следующее фото">›</button></div>`:""}
    </section>
    <div class="sheet-content">
      <div class="sheet-title"><span>${esc(p.kind||cats[p.category])}</span><h2>${esc(p.name)}</h2></div>
      <p class="sheet-description">${esc(p.description)}</p>
      ${p.verified?"":'<p class="hint">Адрес этой сохранённой карточки пока не перепроверен.</p>'}
      <details class="place-more"><summary>Подробнее</summary>
        <dl>${facts.filter(([,value])=>value).map(([label,value])=>`<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>
        <a class="route-link" href="https://www.openstreetmap.org/directions?to=${p.lat},${p.lon}" target="_blank" rel="noopener">Построить маршрут <span>↗</span></a>
        <div class="sheet-links"><a href="https://yandex.ru/maps/?text=${query}" target="_blank" rel="noopener">Яндекс Карты</a>
        <a href="https://2gis.ru/ryazan/search/${query}" target="_blank" rel="noopener">2ГИС</a>
        <a href="${link(p.source)}" target="_blank" rel="noopener">Источник сведений</a></div>
      </details>
    </div>
  </article>`);
  if(photos.length<2)return;
  const track=dialog.querySelector(".photo-track");
  const counter=dialog.querySelector(".photo-count");
  const previous=dialog.querySelector('[data-photo-step="-1"]');
  const next=dialog.querySelector('[data-photo-step="1"]');
  const index=()=>Math.round(track.scrollLeft/track.clientWidth)||0;
  const move=step=>{
    const target=Math.max(0,Math.min(photos.length-1,index()+step));
    track.scrollTo({left:target*track.clientWidth,behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"instant":"smooth"});
  };
  const update=()=>{
    const current=index();
    counter.textContent=`${current+1} / ${photos.length}`;
    previous.disabled=current<=0;
    next.disabled=current>=photos.length-1;
  };
  previous.onclick=()=>move(-1);
  next.onclick=()=>move(1);
  track.addEventListener("scroll",update,{passive:true});
  track.addEventListener("keydown",event=>{
    if(!["ArrowLeft","ArrowRight"].includes(event.key))return;
    event.preventDefault();
    move(event.key==="ArrowRight"?1:-1);
  });
  update();
}
function review(r){
  openDialog(`<form class="stack" data-form="review" data-id="${esc(r.placeData.id)}"><h2>${esc(r.placeData.name)}</h2>
    <p class="muted">Оценка и отзыв по желанию</p><label>Оценка<select name="rating"><option value="">Без оценки</option>
    ${[1,2,3,4,5].map(n=>`<option value="${n}" ${r.rating===n?"selected":""}>${n} из 5</option>`).join("")}</select></label>
    <label>Отзыв<textarea name="review" maxlength="1000" rows="3">${esc(r.review)}</textarea></label><button class="positive">Сохранить</button></form>`);
}
function paint(html){
  main.innerHTML=html; main.setAttribute("aria-busy","false");
  const element=main.querySelector(".card");
  if(!element)return;
  let start;
  element.onpointerdown=e=>{
    if(!e.isPrimary||e.target.closest("button,a"))return;
    start=[e.clientX,e.clientY]; element.setPointerCapture(e.pointerId);
  };
  element.onpointermove=e=>{
    if(!start)return;
    const dx=e.clientX-start[0],dy=e.clientY-start[1];
    const direction=Math.abs(dx)>Math.abs(dy)?(dx>0?"like":"skip"):(dy<0?"visit":"");
    element.dataset.gesture=Math.hypot(dx,dy)>24?direction:"";
    element.style.setProperty("--drag",`${Math.max(-22,Math.min(22,dx*.14))}px`);
  };
  element.onpointercancel=()=>{start=null;element.style.removeProperty("--drag");delete element.dataset.gesture;};
  element.onpointerup=e=>{
    element.style.removeProperty("--drag");delete element.dataset.gesture;
    if(!start)return;
    const dx=e.clientX-start[0],dy=e.clientY-start[1];start=null;
    if(Math.abs(dx)>52&&Math.abs(dx)>Math.abs(dy))action(dx>0?"like":"skip");
    else if(dy < -58 && Math.abs(dy)>Math.abs(dx))action("visit");
    else if(Math.hypot(dx,dy)<8)details(current);
  };
}
async function render(){
  closeCityMap();
  const token=++version;
  clearTimeout(poll);current=null;state=null;
  [page,room]=(location.hash.slice(1)||"feed").split("/");
  if(!["map","feed","saved","visits"].includes(page))page="feed";
  document.body.classList.toggle("feed-page",page==="feed");
  for(const tab of document.querySelectorAll("[data-tab]")){
    if(tab.dataset.tab===(page==="room"?"group":page))tab.setAttribute("aria-current","page");
    else tab.removeAttribute("aria-current");
  }
  try{
    me ||= await api("profile");
    let data;
    if(page==="feed")data=await api("feed",filters);
    if(page==="saved"||page==="visits")data=await api("library");
    if(page==="room")data=await api("rooms/"+encodeURIComponent(room||""));
    if(token!==version)return;
    state=data;
    if(page==="map"){
      paint(cityMapView());
      mountCityMap(token);
    }
    if(page==="feed")paint(feed(data));
    if(page==="saved"||page==="visits")paint(library(data));
    if(page==="group")paint(group());
    if(page==="profile")paint(profile());
    if(page==="room"){paint(roomView(data));schedulePoll(token);}
  }catch(e){
    if(token===version)paint(empty("Не удалось загрузить экран",esc(e.message))+`<div class="controls">${btn("retry","Повторить")}</div>`);
  }
}
function schedulePoll(token){
  poll=setTimeout(async()=>{
    if(token!==version)return;
    if(!busy){
      try{
        const data=await api("rooms/"+encodeURIComponent(room));
        if(token!==version)return;
        if(!busy&&JSON.stringify(data)!==JSON.stringify(state)){state=data;current=null;paint(roomView(data));}
      }catch{if(token===version)toast("Связь с комнатой прервалась. Повторяем запрос.");}
    }
    if(token===version)schedulePoll(token);
  },3000);
}
async function run(fn,button){
  if(busy)return;
  busy=true;if(button)button.disabled=true;
  try{await fn();}
  catch(e){toast(e.message==="Failed to fetch"?"Сервер недоступен. Повторите попытку.":e.message);}
  finally{busy=false;if(button)button.disabled=false;}
}
function action(name,id,button){
  return run(async()=>{
    const token=version,p=find(id);
    if(name==="details")return details(p);
    if(name==="close")return dialog.close();
    if(name==="retry")return render();
    if(name==="review")return review(state.find(r=>r.placeData.id===id));
    if(name==="copy"){
      const field=document.querySelector("#invite");
      try{await navigator.clipboard.writeText(field.value);toast("Ссылка скопирована");}
      catch{field.select();toast("Скопируйте выделенную ссылку");}
      return;
    }
    if(name==="geo"){
      if(filters.origin) delete filters.origin;
      else{
        if(!navigator.geolocation)throw Error("Геолокация недоступна в этом браузере.");
        const pos=await new Promise((yes,no)=>navigator.geolocation.getCurrentPosition(yes,()=>no(Error("Не удалось получить положение. Проверьте разрешение браузера.")),{timeout:8000}));
        filters.origin=[pos.coords.latitude,pos.coords.longitude];
      }
    }
    if(name==="delete"){
      if(!confirm("Удалить профиль, историю и созданные вами комнаты?"))return;
      await api("profile",{},"DELETE");me=null;
    }
    if(name==="logout"){await api("logout",{});me=null;}
    if(name==="undo")await api("undo",{});
    if(name==="unsave")await api("saved",{place:id});
    if(["like","skip","visit"].includes(name)){
      if(!p)return;
      if(page==="feed")await api("swipe",{place:p.id,action:name});
      if(page==="room")await api("rooms/"+room+"/vote",{place:p.id,liked:name==="like"});
      if(name==="like")toast(page==="room"?"Голос сохранён":"Добавлено в «Хочу сходить»");
    }
    if(token!==version)return;
    await render();
    if(name==="visit"&&page==="feed")review({placeData:p});
  },button);
}
document.addEventListener("click",e=>{
  const b=e.target.closest("[data-action]");
  if(b)action(b.dataset.action,b.dataset.id,b);
});
document.addEventListener("keydown",e=>{
  if(e.repeat||dialog.open||!current||e.target.closest("input,select,textarea,button,a"))return;
  const selected={ArrowLeft:"skip",ArrowRight:"like",ArrowUp:"visit"}[e.key];
  if(selected&&(page==="feed"||page==="room"&&selected!=="visit")){
    e.preventDefault();action(selected);
  }
});
document.addEventListener("submit",e=>{
  const type=e.target.dataset.form;
  if(!type)return;
  e.preventDefault();
  run(async()=>{
    const f=new FormData(e.target),token=version;
    if(type==="filter")filters={...filters,...readFilters(f)};
    if(type==="profile"){
      const update={name:f.get("name").trim(),interests:f.getAll("category")};
      await api("profile",update);me={...me,...update};toast("Профиль сохранён");
    }
    if(type==="account"){
      const mode=e.submitter?.value==="login"?"login":"register";
      await api(mode,{nickname:f.get("nickname"),password:f.get("password")});
      me=null;toast(mode==="login"?"Вы вошли":"Аккаунт создан");
    }
    if(type==="create"){
      const result=await api("rooms",{...readFilters(f),name:f.get("name"),count:Number(f.get("count"))});
      if(token===version)location.hash="room/"+result.id;
      return;
    }
    if(type==="join")await api("rooms/"+room+"/join",{name:f.get("name")});
    if(type==="review"){
      await api("visit",{place:e.target.dataset.id,rating:Number(f.get("rating"))||null,review:f.get("review")});
      dialog.close();toast("Посещение обновлено");
    }
    if(token===version)await render();
  },e.submitter);
});
document.addEventListener("error",e=>{
  if(!(e.target instanceof HTMLImageElement))return;
  const credit=e.target.parentElement.querySelector(".photo-credit");
  if(credit){credit.className="no-photo";credit.textContent="Фото не загрузилось";}
  e.target.remove();
},true);
const theme=document.documentElement.dataset;
theme.theme=localStorage.getItem("theme")||"light";
document.querySelector("#theme").onclick=()=>{
  theme.theme=theme.theme==="light"?"dark":"light";localStorage.setItem("theme",theme.theme);
};
window.addEventListener("hashchange",()=>{if(dialog.open)dialog.close();render();});
render();

