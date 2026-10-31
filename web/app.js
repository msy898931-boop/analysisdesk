// Pure helpers (escaping, precision) live in lib.js so the node test runner can
// exercise them directly. Anything that touches the DOM stays here.
import { esc, fmt, basisPct, signedPct } from "./lib.js";

const T = {
  ar:{title:"AnalysisDesk",tagline:"تحليل فني شفّاف على بيانات سوق حقيقية",go:"تحليل",pitch:"العرض",
      reading:"قراءة السوق",agreement:"توافق المؤشرات",entry:"الدخول",sl:"وقف الخسارة",
      tp:"الهدف",rr:"مخاطرة/عائد",support:"الدعم",resistance:"المقاومة",atr:"التقلب (ATR)",
      rsi:"مؤشر القوة RSI",macd:"MACD",stoch:"ستوكاستيك",bands:"بولنجر",
      why:"لماذا هذه القراءة؟",ai:"تفسير الذكاء الاصطناعي",chart:"الشموع (آخر 200)",
      rules:"قاعدة",w:"وزن",notes:"ملاحظات مهمة",live:"بيانات حيّة",of:"من",
      foot:"كل رقم معروض محسوب من بيانات سوق حقيقية. لا توجد أي أرقام مُدخلة يدوياً.",
      foot_src:"المصدر",
      disc:"تحليل تقني آلي. ليس توصية استثمارية ولا ضماناً لأي نتيجة. توافق المؤشرات ليس احتمال ربح.",
      load:"جارٍ قراءة السوق…",neutral:"لا اتجاه واضح",bull:"صاعد",bear:"هابط",mid:"محايد",
      neutral_note:"المؤشرات متعارضة — القراءة الأمينة هي عدم الدخول.",
      sig:"إشارة",agree:"توافق",model:"الموديل",det:"تفسير محسوب",failed:"تعذّر قراءة السوق",
      instrument:"الأداة",spot:"السبوت",futures:"عقد آجل",basis:"الفرق عن السبوت",chart_src:"مصدر الشموع",
      anchor:"الإرساء على السبوت",anchor_na:"غير مُرسّى",
      targets:"الأهداف الستة (خروج تدريجي)",tp_short:"هدف",src:"المصدر",
      lvl:"مستوى ارتكاز",proj:"امتداد تقلّب"},
  en:{title:"AnalysisDesk",tagline:"Transparent technical analysis on real market data",go:"Analyze",pitch:"Pitch",
      reading:"Market read",agreement:"Indicator agreement",entry:"Entry",sl:"Stop loss",
      tp:"Target",rr:"Risk/Reward",support:"Support",resistance:"Resistance",atr:"Volatility (ATR)",
      rsi:"RSI",macd:"MACD",stoch:"Stochastic",bands:"Bollinger",
      why:"Why this read?",ai:"AI explanation",chart:"Candles (last 200)",
      rules:"Rule",w:"Weight",notes:"Important notes",live:"Live data",of:"of",
      foot:"Every figure shown is computed from real market data. No hand-entered numbers.",
      foot_src:"Source",
      disc:"Automated technical analysis. Not investment advice, no guarantee of any outcome. Indicator agreement is not a probability of profit.",
      load:"Reading the market…",neutral:"No clear direction",bull:"bullish",bear:"bearish",mid:"neutral",
      neutral_note:"Indicators conflict — the honest call is to stand aside.",
      sig:"Signal",agree:"Agreement",model:"Model",det:"Computed explanation",failed:"Could not read the market",
      instrument:"Instrument",spot:"Spot",futures:"Futures",basis:"Basis vs spot",chart_src:"Candle source",
      anchor:"Anchored to spot",anchor_na:"Not anchored",
      targets:"Six targets (scale out)",tp_short:"Target",src:"Source",
      lvl:"Pivot level",proj:"Volatility projection"},
};
let LANG="ar", SYMS=[], IVS={}, BUSY=false;

// Bind every element by id instead of leaning on window's named access. That
// shortcut is not safe here: this file is a module (strict mode), and `status`
// is a reserved window property (the status bar string), so a bare `status`
// resolved to a string and `status.textContent=...` threw a TypeError *before*
// the try block -- the panel stayed empty and the button stayed disabled with
// no error shown anywhere. `sym` and `iv` did not resolve at all. Naming each
// one removes the whole class of bug rather than the single symptom.
const $ = id => document.getElementById(id);
const title=$("title"), tagline=$("tagline"), go=$("go"), status=$("status"),
      pitch=$("pitch"),
      sym=$("sym"), iv=$("iv"), out=$("out"), footline=$("footline"), disc=$("disc");

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
  pitch.textContent=t.pitch;
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
  // Name the venue that actually supplied the candles. A fixed "via Yahoo
  // Finance" was a label that lied on spot gold, which comes from Kraken.
  const venue = (d.instrument && (d.instrument.provider || d.instrument.exchange)) || "";
  footline.textContent = t.foot + (venue ? ` ${t.foot_src}: ${venue}.` : "");
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

  // The scale-out ladder. Each rung names where its number came from: a real
  // pivot the market printed, or a stated volatility projection. Collapsing the
  // two into one word "target" would report an assumption as a level.
  const tgts = Array.isArray(s.targets)?s.targets:[];
  const ladder = tgts.length ? `
    <div class="h" style="margin-top:18px"><h2>${t.targets}</h2></div>
    <div class="ladder">${tgts.map(g=>{
      const isLvl = g.kind==="level";
      const src = LANG==="ar"?g.source_ar:g.source_en;
      return `<div class="rung ${isLvl?"lvl":"proj"}">
        <span class="n">${t.tp_short} ${g.index}</span>
        <b class="px">${fmt(g.price)}</b>
        <span class="rr">${t.rr} ${g.rr}</span>
        <span class="tag">${isLvl?t.lvl:t.proj}<i>${esc(src)}</i></span>
      </div>`;
    }).join("")}</div>` : "";

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
    prov += `<span>${t.spot} ${esc(d.spot.metal)}: <b>${fmt(d.spot.price)}</b>`
          + (d.spot.source ? ` <i>${esc(d.spot.source)}</i>` : "") + `</span>`;
    if (d.anchor && d.anchor.ok) {
      // The charted token was rescaled onto this quote, so the levels above are
      // the broker's, not the token's. Name the shift: a silent rescale would be
      // the same hidden substitution this desk refuses everywhere else.
      prov += `<span>${t.anchor}: <b>${signedPct(d.anchor.pct)}</b></span>`;
    } else if (inst.kind==="futures") {
      // On a futures contract the gap to spot is the basis.
      prov += `<span>${t.basis}: <b>${signedPct(basisPct(i.price, d.spot.price))}</b></span>`;
    } else if (d.anchor) {
      prov += `<span class="provmiss">${t.anchor_na}</span>`;
    }
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
      ${ladder}
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
    const r=await fetch(`/api/candles?symbol=${encodeURIComponent(last.symbol)}&interval=${last.interval}&lang=${LANG}`);
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
