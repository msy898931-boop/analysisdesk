// Pure helpers (escaping, precision) live in lib.js so the node test runner can
// exercise them directly. Anything that touches the DOM stays here.
import { esc, fmt, basisPct, signedPct } from "./lib.js";

const T = {
  ar:{title:"AnalysisDesk",tagline:"تحليل فني شفّاف على بيانات سوق حقيقية",go:"تحليل",
      reading:"قراءة السوق",agreement:"توافق المؤشرات",entry:"الدخول",sl:"وقف الخسارة",
      tp:"الهدف",rr:"مخاطرة/عائد",support:"الدعم",resistance:"المقاومة",atr:"التقلب (ATR)",
      rsi:"مؤشر القوة RSI",macd:"MACD",stoch:"ستوكاستيك",bands:"بولنجر",
      why:"لماذا هذه القراءة؟",ai:"تفسير الذكاء الاصطناعي",chart:"الشموع (آخر 200)",
      rules:"قاعدة",w:"وزن",notes:"ملاحظات مهمة",live:"بيانات حيّة",of:"من",
      foot:"كل رقم معروض محسوب من بيانات سوق حقيقية عبر Yahoo Finance. لا توجد أي أرقام مُدخلة يدوياً.",
      disc:"تحليل تقني آلي. ليس توصية استثمارية ولا ضماناً لأي نتيجة. توافق المؤشرات ليس احتمال ربح.",
      load:"جارٍ قراءة السوق…",neutral:"لا اتجاه واضح",bull:"صاعد",bear:"هابط",mid:"محايد",
      neutral_note:"المؤشرات متعارضة — القراءة الأمينة هي عدم الدخول.",
      sig:"إشارة",agree:"توافق",model:"الموديل",det:"تفسير محسوب",failed:"تعذّر قراءة السوق",
      instrument:"الأداة",spot:"السبوت",futures:"عقد آجل",basis:"الفرق عن السبوت",track:"مطابقة السبوت",chart_src:"مصدر الشموع"},
  en:{title:"AnalysisDesk",tagline:"Transparent technical analysis on real market data",go:"Analyze",
      reading:"Market read",agreement:"Indicator agreement",entry:"Entry",sl:"Stop loss",
      tp:"Target",rr:"Risk/Reward",support:"Support",resistance:"Resistance",atr:"Volatility (ATR)",
      rsi:"RSI",macd:"MACD",stoch:"Stochastic",bands:"Bollinger",
      why:"Why this read?",ai:"AI explanation",chart:"Candles (last 200)",
      rules:"Rule",w:"Weight",notes:"Important notes",live:"Live data",of:"of",
      foot:"Every figure shown is computed from real market data via Yahoo Finance. No hand-entered numbers.",
      disc:"Automated technical analysis. Not investment advice, no guarantee of any outcome. Indicator agreement is not a probability of profit.",
      load:"Reading the market…",neutral:"No clear direction",bull:"bullish",bear:"bearish",mid:"neutral",
      neutral_note:"Indicators conflict — the honest call is to stand aside.",
      sig:"Signal",agree:"Agreement",model:"Model",det:"Computed explanation",failed:"Could not read the market",
      instrument:"Instrument",spot:"Spot",futures:"Futures",basis:"Basis vs spot",track:"Tracking spot",chart_src:"Candle source"},
};
let LANG="ar", SYMS=[], IVS={}, BUSY=false;

function setLang(l){
  LANG=l;
  document.documentElement.lang=l;
  document.documentElement.dir = l==="ar"?"rtl":"ltr";
  document.getElementById("l-ar").className = l==="ar"?"on":"";
  document.getElementById("l-en").className = l==="en"?"on":"";
  document.getElementById("l-ar").setAttribute("aria-pressed", l==="ar"?"true":"false");
  document.getElementById("l-en").setAttribute("aria-pressed", l==="en"?"true":"false");
  const t=T[l];
  title.textContent=t.title; tagline.textContent=t.tagline;
  go.textContent=t.go; footline.textContent=t.foot; disc.textContent=t.disc;
  renderChips();
  if(last) paint(last);
}

let last=null;

function renderChips(){
  const t=T[LANG], box=document.getElementById("chips");
  box.innerHTML="";
  SYMS.forEach(s=>{
    const b=document.createElement("button");
    b.className="chip";
    b.textContent = LANG==="ar"?s.name_ar:s.name_en;
    b.onclick=()=>{sym.value=s.symbol; run();};
    box.appendChild(b);
  });
}

function fillSelects(){
  sym.innerHTML=""; iv.innerHTML="";
  const groups={};
  SYMS.forEach(s=>{ (groups[s.class]=groups[s.class]||[]).push(s); });
  const gnames={forex:{ar:"عملات",en:"Forex"},crypto:{ar:"كريبتو",en:"Crypto"},
    commodity:{ar:"سلع",en:"Commodities"},index:{ar:"مؤشرات",en:"Indices"},stock:{ar:"أسهم",en:"Stocks"}};
  Object.keys(groups).forEach(g=>{
    const og=document.createElement("optgroup");
    og.label=(gnames[g]||{})[LANG]||g;
    groups[g].forEach(s=>{
      const o=document.createElement("option");
      o.value=s.symbol; o.textContent=(LANG==="ar"?s.name_ar:s.name_en)+" · "+s.symbol;
      og.appendChild(o);
    });
    sym.appendChild(og);
  });
  Object.keys(IVS).forEach(k=>{
    const o=document.createElement("option");
    o.value=k; o.textContent=LANG==="ar"?IVS[k].ar:IVS[k].en;
    iv.appendChild(o);
  });
  sym.value="EURUSD=X"; iv.value="1h";
}

// The panel is built with innerHTML, so an unescaped value from the API would
// be parsed as markup. esc() lives in lib.js (shared with the node tests).

async function boot(){
  try{
    const r=await fetch("/api/symbols"); const d=await r.json();
    SYMS=d.symbols; IVS=d.intervals; fillSelects(); renderChips();
  }catch(e){ /* selects stay empty; run() will surface the error */ }
  setLang("ar");
  // Open on a live read rather than an empty panel.
  run();
}

async function run(){
  if(BUSY) return; BUSY=true;
  const t=T[LANG];
  go.disabled=true; status.textContent=t.load;
  out.innerHTML='<div class="card pad" style="margin-top:16px"><div class="skel"></div></div>';
  try{
    const u=`/api/analyze?symbol=${encodeURIComponent(sym.value)}&interval=${iv.value}&lang=${LANG}`;
    const r=await fetch(u);
    if(!r.ok){ const j=await r.json().catch(()=>({})); throw new Error(j.detail||("HTTP "+r.status)); }
    last=await r.json(); paint(last);
    status.textContent="";
  }catch(e){
    out.innerHTML=`<div class="card pad" style="margin-top:16px"><div class="err">
      <b>${t.failed}</b><br>${esc((e.message||e)+"")}</div></div>`;
    status.textContent="";
  }finally{ go.disabled=false; BUSY=false; }
}

// A canvas is invisible to a screen reader, so it carries a text description of
// what it draws: the range and the last close. The same text is printed in an
// sr-only paragraph so the description is reachable, not just an attribute.
function chartAlt(d, s){
  const i=d.indicators;
  const hi=fmt(i.resistance), lo=fmt(i.support);
  return LANG==="ar"
    ? `رسم شموع ${d.symbol} على فريم ${d.interval}: النطاق بين دعم ${lo} ومقاومة ${hi}، وآخر سعر ${fmt(i.price)}.`
    : `Candlestick chart for ${d.symbol} on ${d.interval}: range between support ${lo} and resistance ${hi}, last price ${fmt(i.price)}.`;
}

function paint(d){
  const t=T[LANG], i=d.indicators, s=d.signal;
  const dirTxt = s.direction==="BUY"?t.bull : s.direction==="SELL"?t.bear : t.neutral;
  const dirCls = s.direction;
  const up = i.change_pct>=0;

  const rules = s.rules.filter(r=>r.weight>0).map(r=>{
    const col = r.vote==="bullish"?"var(--up)":r.vote==="bearish"?"var(--down)":"var(--dim)";
    const vt = r.vote==="bullish"?t.bull : r.vote==="bearish"?t.bear : t.mid;
    const dt = LANG==="ar"?r.detail_ar:r.detail_en;
    return `<div class="rule">
      <span class="dot" style="background:${col}"></span>
      <span class="txt"><b>${esc(r.name)}</b><i>${esc(dt)}</i></span>
      <span class="w">${vt} · +${r.weight}</span>
    </div>`;
  }).join("");

  const warns = s.warnings.map(w=>`<div class="warn">⚠ ${esc(LANG==="ar"?w.ar:w.en)}</div>`).join("");

  const aiMeta = d.narrative.source==="model"
      ? `<span>${t.model}: ${esc(d.narrative.model)}</span>`
      : `<span>${t.det}${d.narrative.note?(" · "+esc(d.narrative.note)):""}</span>`;

  // Say exactly which instrument this number is. A futures contract and the
  // spot it tracks differ, and hiding that is how a wrong-looking price happens.
  const inst = d.instrument || {};
  let prov = `<div class="prov">`;
  if (inst.name) {
    prov += `<span>${t.instrument}: <b>${esc(inst.name)}</b></span>`;
    prov += `<span>${inst.kind==="futures"?t.futures:t.spot}</span>`;
  }
  if (inst.exchange) prov += `<span>${esc(inst.exchange)}</span>`;
  if (d.spot && d.spot.price) {
    const basis = basisPct(i.price, d.spot.price);
    prov += `<span>${t.spot} ${esc(d.spot.metal)}: <b>${fmt(d.spot.price)}</b>`
          + (d.spot.source ? ` <i>${esc(d.spot.source)}</i>` : "") + `</span>`;
    // On a futures contract this gap is the basis; on the spot instrument it is
    // how closely the charted market tracks the live broker quote.
    const label = inst.kind==="futures" ? t.basis : t.track;
    prov += `<span>${label}: <b>${signedPct(basis)}</b></span>`;
  } else if (d.spot && d.spot.error) {
    prov += `<span class="provmiss">${t.spot}: ${t.failed}</span>`;
  }
  prov += `</div>`;

  out.innerHTML = `
  <div class="grid">
    <div class="card pad">
      <div class="h"><h2>${t.reading} · ${esc(d.symbol)}</h2>
        <span class="badge b-live">${t.live}</span></div>
      ${prov}
      <div class="verdict">
        <div>
          <div class="dir ${dirCls}">${dirTxt}</div>
          <div class="chg" style="color:${i.change_pct>0?'var(--up)':i.change_pct<0?'var(--down)':'var(--dim)'}">
            ${i.change_pct>0?"▲":i.change_pct<0?"▼":"•"} ${Math.abs(i.change_pct).toFixed(2)}%</div>
        </div>
        <div style="flex:1">
          <div class="price">${fmt(i.price)} <span style="font-size:12px;color:var(--dim)">${esc(d.currency||"")}</span></div>
          <div class="sub" style="color:var(--dim);font-size:11.5px;margin-top:4px">
            ${t.agreement}: ${s.confidence}%  ·  ${esc(d.exchange||"")}</div>
          <div class="meter"><i style="width:${Math.max(s.confidence,2)}%"></i></div>
        </div>
      </div>
      <div class="kv">
        <div><span>${t.entry}</span><b>${fmt(s.entry)}</b></div>
        <div><span>${t.sl}</span><b style="color:var(--down)">${fmt(s.stop_loss)}</b></div>
        <div><span>${t.tp}</span><b style="color:var(--up)">${fmt(s.take_profit)}</b></div>
        <div><span>${t.rr}</span><b>${s.risk_reward}</b></div>
        <div><span>${t.support}</span><b>${fmt(i.support)}</b></div>
        <div><span>${t.resistance}</span><b>${fmt(i.resistance)}</b></div>
      </div>
      <div class="h" style="margin-top:18px"><h2>${t.chart}</h2>
        <span class="sub" style="color:var(--dim);font-size:11.5px">${esc(d.interval)}</span></div>
      <canvas id="cv" role="img" aria-label="${esc(chartAlt(d, s))}"></canvas>
      <p class="sr-only">${esc(chartAlt(d, s))}</p>
      ${warns?`<div class="warns">${warns}</div>`:""}
    </div>

    <div style="display:flex;flex-direction:column;gap:16px">
      <div class="card pad">
        <div class="h"><h2>${t.ai}</h2></div>
        <div class="ai">
          <div class="meta">${aiMeta}<span>${t.agree}: ${s.confidence}%</span></div>
          ${esc(d.narrative.text).replace(/\n/g,"<br>")}
        </div>
      </div>

      <div class="card pad">
        <div class="h"><h2>${t.why}</h2><span class="sub" style="color:var(--dim);font-size:11.5px">${t.sig}: ${s.direction} · ${s.score>0?"+":""}${s.score}</span></div>
        <div class="rules">${rules||`<div class="rule"><span class="dot"></span><span class="txt"><i>${t.neutral_note}</i></span><span class="w"></span></div>`}</div>
      </div>

      <div class="card pad">
        <div class="h"><h2>${t.rsi} / ${t.macd}</h2></div>
        <div class="kv">
          <div><span>${t.rsi}</span><b>${i.rsi}</b></div>
          <div><span>${t.macd}</span><b>${i.macd_hist}</b></div>
          <div><span>${t.stoch}</span><b>${i.stoch_k}</b></div>
          <div><span>${t.atr}</span><b>${i.atr_pct}%</b></div>
          <div><span>${t.bands}</span><b>${fmt(i.bb_lower)} – ${fmt(i.bb_upper)}</b></div>
          <div><span>EMA200</span><b>${fmt(i.ema200)}</b></div>
        </div>
      </div>
    </div>
  </div>`;

  drawChart();
}

async function drawChart(){
  const cv=document.getElementById("cv"); if(!cv) return;
  let data;
  try{
    const r=await fetch(`/api/candles?symbol=${encodeURIComponent(last.symbol)}&interval=${last.interval}`);
    if(!r.ok) return;                       // chart is a nicety; the read is not
    data=(await r.json()).candles;
  }catch(e){ return; }
  if(!Array.isArray(data)||!data.length) return;

  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth, H=260;
  cv.width=W*dpr; cv.height=H*dpr;
  const x=cv.getContext("2d"); x.scale(dpr,dpr);
  x.clearRect(0,0,W,H);

  // Room on the right for the price labels and at the bottom for the dates.
  const padL=8, padR=58, padT=10, padB=22;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const lo=Math.min(...data.map(c=>c.l)), hi=Math.max(...data.map(c=>c.h));
  const span=hi-lo||1;
  const Y=v=>padT+(hi-v)/span*plotH;
  const bw=plotW/data.length;

  x.font="10px ui-monospace,SFMono-Regular,Menlo,monospace";
  x.textBaseline="middle";

  // Grid + price axis. A chart with no numbers is decoration; the reader has to
  // be able to read a level off it.
  x.strokeStyle="rgba(255,255,255,.05)"; x.lineWidth=1;
  x.fillStyle="rgba(125,139,154,.9)";
  for(let g=0; g<=4; g++){
    const y=padT+g*plotH/4, v=hi-(hi-lo)*g/4;
    x.beginPath(); x.moveTo(padL,y); x.lineTo(padL+plotW,y); x.stroke();
    x.textAlign="left";
    x.fillText(fmt(v), padL+plotW+6, y);
  }

  // Candles.
  data.forEach((c,k)=>{
    const cx=padL+k*bw, o=Y(c.o), cl=Y(c.c);
    const up=c.c>=c.o, col=up?"#26a269":"#e5534b";
    x.strokeStyle=col; x.fillStyle=col;
    x.beginPath(); x.moveTo(cx+bw/2, Y(c.h)); x.lineTo(cx+bw/2, Y(c.l)); x.stroke();
    const top=Math.min(o,cl), hgt=Math.max(Math.abs(cl-o),1);
    x.fillRect(cx+bw*0.15, top, Math.max(bw*0.7,1), hgt);
  });

  // Time axis: first, middle and last candle. Locale-aware, day+month for the
  // daily/weekly frames and hh:mm for the intraday ones.
  const intraday = !["1d","1wk"].includes(last.interval);
  const stamp = t => new Date(t*1000).toLocaleString(LANG==="ar"?"ar-EG":"en-GB",
      intraday ? {hour:"2-digit",minute:"2-digit"} : {day:"2-digit",month:"short"});
  x.fillStyle="rgba(125,139,154,.9)"; x.textBaseline="top";
  [[0,"left"],[Math.floor(data.length/2),"center"],[data.length-1,"right"]].forEach(([i,al])=>{
    const px = i===0?padL : i===data.length-1?padL+plotW : padL+i*bw+bw/2;
    x.textAlign=al;
    x.fillText(stamp(data[i].t), px, padT+plotH+6);
  });
}

document.getElementById("go").onclick=run;
document.getElementById("l-ar").onclick=()=>setLang("ar");
document.getElementById("l-en").onclick=()=>setLang("en");
boot();
