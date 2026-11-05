"""Dashboard page /sim: time-machine mock investing (data from replay.py, kv["replay"]).

The ranking and notes are rendered on the server; the replay player (racing equity chart, live leaderboard,
learning log, trade feed, holdings) runs in the browser from /api/sim.
"""
from __future__ import annotations

import json

from . import labels as LB
from .webui import card, empty, h, money, pct, tone

CHART_N = 5  # methods drawn on the chart (+ benchmark)


def load(db) -> dict | None:
    raw = db.get("replay")
    return json.loads(raw) if raw else None


def _dt(s: str) -> str:
    return f"{s[:4]}.{s[5:7]}.{s[8:10]}"


def charted(R: dict) -> list[str]:
    return [m["id"] for m in R["methods"][:CHART_N]]


def page_body(R: dict | None) -> str:
    if not R:
        return card("타임머신 모의투자", empty("⏳", "아직 계산 전이에요. 서버에서 <code>stockmining replay</code> 가 끝나면 나타나요."))
    ms, amt = R["methods"], R["amount"]
    top = ms[0]
    learner = next((m for m in ms if m["id"] == "learner"), None)
    b = R.get("bench") or {}
    bt = (b.get("metrics") or {}).get("total_pct")
    out = [f'<section class="hero"><small>{_dt(R["start"])} ~ {_dt(R["end"])} · 가상 {amt // 10_000:,}만원으로 시작</small>'
           f'<span class="lbl">가장 많이 번 방식</span><b class="big" style="font-size:28px">{h(top["label"])}</b>'
           f'<p><span class="{tone(top["metrics"]["total_pct"])}">{pct(top["metrics"]["total_pct"], 1)}</span>'
           f' · {money(amt * (1 + top["metrics"]["total_pct"] / 100), "KRW")}'
           + (f' · {h(b["name"])} {pct(bt, 1)}' if bt is not None else "") + '</p>'
           f'<p class="desc">과거로 돌아가 하루씩 앞으로 가며 투자해 봤어요. 매일 <b>그날 장 마감까지 알 수 있던 것만</b> 보고 '
           f'다음 날 사고팔았고, 분기마다 <b>이미 끝난 거래만</b> 보고 쓸 전략을 다시 골랐어요. '
           f'결과를 보고 고른 방식은 하나도 없어요.</p></section>']

    out.append(
        '<section class="card" id="sim"><div class="simhead"><div><b id="simdate">-</b><small id="simday"></small></div>'
        '<em id="simreg"></em></div>'
        '<div id="simchart" class="simchart" role="img" aria-label="방식별 자산 변화"></div>'
        '<div class="simctl"><button id="simplay" type="button" aria-label="재생">▶</button>'
        '<input id="simrange" type="range" min="0" value="0" aria-label="날짜 이동">'
        '<div class="pills sp" id="simspeed"><a data-v="6">느리게</a><a data-v="20" class="on">보통</a><a data-v="70">빠르게</a></div></div>'
        '<div id="simboard" class="simboard"></div>'
        '<p class="lead" style="margin-top:6px">줄을 누르면 그 방식의 매매 기록과 보유 종목을 아래에서 볼 수 있어요.</p></section>'
        '<section class="card"><h2 id="simfocus">매매 기록</h2><div id="simhold"></div><ul class="plain feed" id="simfeed"></ul></section>'
        '<section class="card"><h2>🧠 학습 일지 <small id="simq"></small></h2><p class="lead" id="simlearnlead"></p>'
        '<div id="simlearn"></div><p class="lead" id="simml" style="margin-top:8px"></p></section>')

    rows = ""
    for i, m in enumerate(ms, 1):
        x = m["metrics"]
        rows += (f'<tr><td>{i}. <b>{h(m["label"])}</b><small class="sub">{h(m.get("desc", ""))}</small>'
                 f'<small class="sub">{m["slots"]}종목 · 거래 {x["trades"]}번'
                 f'{" · 이긴 비율 " + pct(x["win_rate"], 0, False) if x["win_rate"] is not None else ""}</small></td>'
                 f'<td class="{tone(x["total_pct"])}">{pct(x["total_pct"], 1)}</td><td>{pct(x["cagr_pct"], 1)}</td>'
                 f'<td class="down">-{x["mdd_pct"]:.0f}%</td></tr>')
    if b.get("metrics"):
        rows += (f'<tr class="ref"><td>📈 {h(b["name"])} 그냥 들고 있기<small class="sub">비교 기준</small></td>'
                 f'<td class="{tone(bt)}">{pct(bt, 1)}</td><td>-</td><td class="down">-{b["metrics"]["mdd_pct"]:.0f}%</td></tr>')
    rnd = R.get("random")
    if rnd:
        rows += (f'<tr class="ref"><td>🎲 아무거나 골랐을 때<small class="sub">같은 신호를 무작위로 {rnd["runs"]}번 · 중앙값 '
                 f'(10~90%: {pct(rnd["p10_pct"], 0)} ~ {pct(rnd["p90_pct"], 0)})</small></td>'
                 f'<td class="{tone(rnd["median_pct"])}">{pct(rnd["median_pct"], 1)}</td><td>-</td><td>-</td></tr>')
    out.append(card("최종 순위", '<p class="lead">총수익 순서예요. 최대 낙폭은 고점에서 가장 많이 빠졌던 정도예요.</p>'
                    '<div class="tscroll"><table class="t rank"><thead><tr><th>방식</th><th>총수익</th><th>1년 환산</th>'
                    f'<th>최대 낙폭</th></tr></thead><tbody>{rows}</tbody></table></div>', count=f"{len(ms)}개"))

    notes = []
    if bt is not None:
        beat = [m for m in ms if m["metrics"]["total_pct"] > bt]
        notes.append(f"{h(b['name'])}을 그냥 들고 있었으면 {pct(bt, 1)}였어요. 이보다 많이 번 방식은 {len(ms)}개 중 {len(beat)}개예요"
                     + (f" ({', '.join(h(m['label']) for m in beat)})." if beat else "."))
    if rnd and learner:
        diff = learner["metrics"]["total_pct"] - rnd["median_pct"]
        notes.append(f"지금 실제로 쓰는 AI 학습기는 같은 신호를 아무거나 고른 경우보다 {abs(diff):.1f}%p "
                     f"{'더 벌었어요' if diff > 0 else '덜 벌었어요'}. "
                     + ("차이가 작아서 실력이라고 하기엔 아직 일러요." if 0 < diff < 15 else
                        "고르는 실력이 운보다 낫다는 뜻이에요." if diff > 0 else "아직은 고르는 실력이 운과 구분이 안 돼요."))
    picks = {}
    for q in R.get("learn", []):
        if q.get("best"):
            picks[q["best"]] = picks.get(q["best"], 0) + 1
    if picks:
        sk = R.get("strat_ko", {})
        notes.append("분기 1등 따라가기가 고른 전략: " + ", ".join(
            f"{h(sk.get(n, n))} {c}번" for n, c in sorted(picks.items(), key=lambda x: -x[1])) + " (분기 기준).")
    wins = sorted((e for e in top["events"] if e[1] == "s" and e[5] is not None), key=lambda e: -e[5])[:3]
    if wins:
        notes.append(f"1등 {h(top['label'])}의 가장 큰 수익 거래: " + ", ".join(f"{h(e[2])} {pct(e[5], 0)}" for e in wins)
                     + ". 몇 번의 큰 수익이 성적을 많이 좌우했어요.")
    notes += ["전략 규칙의 숫자는 처음 정한 기본값 그대로예요. 나중에 전체 기간을 보고 고친 값은 쓰지 않았어요.",
              "거래 비용(수수료·슬리피지)은 넣었고, 세금과 환율 변화는 넣지 않았어요.",
              "지금 상장된 종목 위주의 데이터라 상장폐지된 종목이 빠져 있어요(생존 편향). 실제로는 이보다 조금 덜 벌 수 있어요."]
    out.append(card("꼭 알아두세요", '<ul class="plain">' + "".join(f'<li class="note">{n}</li>' for n in notes) + "</ul>"))
    out.append(f'<p class="updated">{h(R["ts"][:16].replace("T", " "))} (뉴욕 시간) 계산 · 매주 학습 때 다시 계산해요</p>')
    out.append(SIM_CSS + SIM_JS.replace("{{charted}}", json.dumps(charted(R))))
    return "".join(out)


def home_card(R: dict | None) -> str:
    if not R:
        return ""
    top = R["methods"][0]
    learner = next((m for m in R["methods"] if m["id"] == "learner"), None)
    sub = f'AI 학습기 {pct(learner["metrics"]["total_pct"], 1)}' if learner else ""
    return card("⏱️ 타임머신 모의투자",
                f'<a class="row" href="/sim"><span class="av">▶</span><span class="rt"><b>{h(top["label"])}</b>'
                f'<small>{_dt(R["start"])}부터 가상 투자 1등 · {sub}</small></span>'
                f'<span class="rr"><b class="{tone(top["metrics"]["total_pct"])}">{pct(top["metrics"]["total_pct"], 1)}</b>'
                f'<small>총수익</small></span><span class="go"></span></a>', ("/sim", "재생"))


SIM_CSS = """<style>
.simhead{display:flex;justify-content:space-between;align-items:flex-start}.simhead b{font-size:24px;font-variant-numeric:tabular-nums;display:block}
.simhead small{color:var(--ink3);font-size:13px}
.simchart{position:relative;height:230px;margin:6px 0 4px;touch-action:pan-y}.simchart svg{display:block;width:100%;height:100%;overflow:visible}
.simchart .g{stroke:var(--line);stroke-width:1}.simchart .gl{fill:var(--ink3);font-size:11px;font-variant-numeric:tabular-nums}
.simchart .base{stroke:var(--ink3);stroke-width:1;stroke-dasharray:4 4}.simchart .q{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3}
.simchart .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}.simchart .ln.f{stroke-width:3}
.simchart .ln.bench{stroke:var(--ink3);stroke-dasharray:5 4;stroke-width:1.5}.simchart .dot{stroke:var(--card);stroke-width:2}
.simchart .cross{stroke:var(--ink3);stroke-width:1}
.simtip{position:absolute;top:0;background:var(--ink);color:var(--card);font-size:12px;padding:6px 9px;border-radius:10px;pointer-events:none;
font-variant-numeric:tabular-nums;line-height:1.5;white-space:nowrap;z-index:2}.simtip i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px}
.simctl{display:flex;align-items:center;gap:10px;margin:6px 0 8px;flex-wrap:wrap}
.simctl button{flex:0 0 44px;height:44px;border-radius:50%;border:0;background:var(--accent);color:#fff;font-size:17px;cursor:pointer}
.simctl input{flex:1;min-width:120px;accent-color:var(--accent)}.pills.sp{margin:0}.pills.sp a{cursor:pointer;background:var(--fill);padding:6px 11px;font-size:13px}
.pills.sp a.on{background:var(--ink);color:var(--card)}
.simboard .row{padding:9px 8px}.simboard .row.on{background:var(--fill)}.simboard .rk{flex:0 0 22px;font-weight:700;color:var(--ink3);font-variant-numeric:tabular-nums}
.sw{flex:0 0 12px;height:12px;border-radius:3px;background:var(--line)}.sw.bench{background:none;border:2px dashed var(--ink3)}
.simboard .rt b{font-size:15px}.simboard .rr b{font-size:15px}
.feed li.note{display:flex;gap:10px;align-items:baseline;padding:9px 0}.feed .d{flex:0 0 46px;color:var(--ink3);font-size:13px;font-variant-numeric:tabular-nums}
.feed .x{flex:1;min-width:0}.feed small{display:block;color:var(--ink3);font-size:12.5px}
.hold{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 6px}.hold span{background:var(--fill);border-radius:10px;padding:6px 10px;font-size:13px}
.hold span small{color:var(--ink3);margin-left:4px}
.lrn{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line);font-size:14.5px}.lrn:last-child{border:0}
.lrn .rt small{font-size:12.5px}.lrn.off{opacity:.55}
table.rank td small.sub{display:block;color:var(--ink3);font-size:12px;font-weight:400}table.rank td{vertical-align:top}
table.rank tr.ref td{color:var(--ink2)}.tscroll{overflow-x:auto}
</style>"""

SIM_JS = r"""<script>
(function(){
var CH={{charted}},NS="http://www.w3.org/2000/svg",R,di=0,playing=false,speed=20,acc=0,lastT=0,focus=null;
var COL_L=["#2a78d6","#eb6834","#1baf7a","#eda100","#e87ba4"],COL_D=["#3987e5","#d95926","#199e70","#c98500","#d55181"];
var dark=matchMedia("(prefers-color-scheme: dark)");function col(i){return (dark.matches?COL_D:COL_L)[i%5]}
var REG={u:["상승장","good"],n:["보통",""],d:["하락장","bad"]};
var WHY={stop:"손절",stop_gap:"손절(갭)",target:"목표가",target_gap:"목표가(갭)",rule:"조건 청산",time:"기간 만료"};
var $=function(i){return document.getElementById(i)};
function won(v){return Math.round(v).toLocaleString("ko-KR")+"원"}
function pc(v,d){return (v>0?"+":"")+v.toFixed(d==null?1:d)+"%"}
function tn(e,v){e.classList.remove("up","down");if(v>0)e.classList.add("up");else if(v<0)e.classList.add("down")}
function dt(s){return s.slice(2).replace(/-/g,".")}
function el(t,a){var e=document.createElementNS(NS,t);for(var k in a)e.setAttribute(k,a[k]);return e}
fetch(location.pathname.indexOf("/kr/")===0?"/api/sim?m=kr":"/api/sim",{credentials:"same-origin"}).then(function(r){return r.json()}).then(init).catch(function(){
  $("simdate").textContent="불러오지 못했어요"});
function init(d){R=d;R.by={};R.methods.forEach(function(m,i){R.by[m.id]=m;m.rank0=i});
  CH=CH.filter(function(id){return R.by[id]});CH.forEach(function(id,i){R.by[id].ci=i});
  var r=$("simrange");r.max=R.dates.length-1;di=R.dates.length-1;r.value=di;
  r.addEventListener("input",function(){di=+r.value;stop();render()});
  $("simplay").addEventListener("click",function(){if(playing)return stop();if(di>=R.dates.length-1)di=0;playing=true;
    $("simplay").textContent="❚❚";$("simplay").setAttribute("aria-label","일시정지");lastT=0;requestAnimationFrame(tick)});
  document.querySelectorAll("#simspeed a").forEach(function(a){a.addEventListener("click",function(){speed=+a.dataset.v;
    document.querySelectorAll("#simspeed a").forEach(function(b){b.classList.toggle("on",b===a)})})});
  focus="learner" in R.by?"learner":R.methods[0].id;
  window.addEventListener("resize",function(){render()});dark.addEventListener&&dark.addEventListener("change",render);
  render()}
function stop(){playing=false;$("simplay").textContent="▶";$("simplay").setAttribute("aria-label","재생")}
function tick(t){if(!playing)return;if(lastT){acc+=(t-lastT)/1000*speed;var k=Math.floor(acc);if(k>0){acc-=k;di=Math.min(R.dates.length-1,di+k);
  $("simrange").value=di;render();if(di>=R.dates.length-1){stop();return}}}lastT=t;requestAnimationFrame(tick)}
function render(){
  $("simdate").textContent=R.dates[di].replace(/-/g,".");$("simday").textContent=(di+1)+"일째 / "+R.dates.length+"거래일";
  var rg=REG[R.regime[di]]||REG.n,re=$("simreg");re.textContent="시장 "+rg[0];re.className=rg[1];
  chart();board();feed();learn()}
function series(){var s=CH.map(function(id){return {id:id,m:R.by[id],eq:R.by[id].eq,c:col(R.by[id].ci)}});
  if(focus&&CH.indexOf(focus)<0)s.push({id:focus,m:R.by[focus],eq:R.by[focus].eq,c:getComputedStyle(document.body).color,f:1});
  return s}
function chart(){
  var box=$("simchart");box.innerHTML="";var W=box.offsetWidth,H=box.offsetHeight;if(!W)return;
  var S=series(),B=R.bench&&R.bench.eq,n=R.dates.length,padL=4,padR=46,padT=8,padB=18;
  var lo=1,hi=1;S.forEach(function(s){s.eq.forEach(function(v){if(v<lo)lo=v;if(v>hi)hi=v})});
  if(B)B.forEach(function(v){if(v<lo)lo=v;if(v>hi)hi=v});var sp=hi-lo||.1;lo-=sp*.05;hi+=sp*.05;sp=hi-lo;
  var X=function(i){return padL+i/(n-1)*(W-padL-padR)},Y=function(v){return padT+(hi-v)/sp*(H-padT-padB)};
  var svg=el("svg",{viewBox:"0 0 "+W+" "+H});
  var step=[.05,.1,.2,.25,.5,1,2,5].find(function(x){return sp/x<=5})||10;
  for(var g=Math.ceil(lo/step)*step;g<=hi;g+=step){var y=Y(g);if(Math.abs(g-1)<1e-9)continue;
    svg.appendChild(el("line",{x1:padL,x2:W-padR,y1:y,y2:y,"class":"g"}));
    var t=el("text",{x:W-padR+6,y:y+4,"class":"gl"});t.textContent=pc((g-1)*100,0);svg.appendChild(t)}
  svg.appendChild(el("line",{x1:padL,x2:W-padR,y1:Y(1),y2:Y(1),"class":"base"}));
  var t0=el("text",{x:W-padR+6,y:Y(1)+4,"class":"gl"});t0.textContent="원금";svg.appendChild(t0);
  R.learn.forEach(function(q){if(q.di>0)svg.appendChild(el("line",{x1:X(q.di),x2:X(q.di),y1:padT,y2:H-padB,"class":"q"}))});
  [0,Math.floor((n-1)/2),n-1].forEach(function(i,k){var tx=el("text",{x:X(i),y:H-3,"class":"gl","text-anchor":["start","middle","end"][k]});
    tx.textContent=dt(R.dates[i]);svg.appendChild(tx)});
  function path(eq){var p="";for(var i=0;i<=di;i++)p+=(i?"L":"M")+X(i).toFixed(1)+" "+Y(eq[i]).toFixed(1);return p}
  if(B)svg.appendChild(el("path",{d:path(B),"class":"ln bench"}));
  S.forEach(function(s){svg.appendChild(el("path",{d:path(s.eq),"class":"ln"+(s.id===focus?" f":""),stroke:s.c}))});
  S.forEach(function(s){svg.appendChild(el("circle",{cx:X(di),cy:Y(s.eq[di]),r:4,fill:s.c,"class":"dot"}))});
  var cross=el("line",{y1:padT,y2:H-padB,"class":"cross",visibility:"hidden"});svg.appendChild(cross);box.appendChild(svg);
  var tip=document.createElement("div");tip.className="simtip";tip.style.display="none";box.appendChild(tip);
  function show(e){var r=box.getBoundingClientRect(),i=Math.round((e.clientX-r.left-padL)/(W-padL-padR)*(n-1));
    i=Math.max(0,Math.min(di,i));var px=X(i);cross.setAttribute("x1",px);cross.setAttribute("x2",px);cross.setAttribute("visibility","visible");
    var rows=S.map(function(s){return [s.eq[i],'<i style="background:'+s.c+'"></i>'+s.m.label+" "+pc((s.eq[i]-1)*100)]});
    if(B)rows.push([B[i],'<i style="border:1px dashed currentColor"></i>'+R.bench.name+" "+pc((B[i]-1)*100)]);
    rows.sort(function(a,b){return b[0]-a[0]});
    tip.innerHTML="<b>"+R.dates[i].replace(/-/g,".")+"</b><br>"+rows.map(function(x){return x[1]}).join("<br>");
    tip.style.display="block";var tw=tip.offsetWidth;tip.style.left=Math.max(0,Math.min(W-tw,px+(px>W/2?-tw-10:10)))+"px"}
  function hide(){cross.setAttribute("visibility","hidden");tip.style.display="none"}
  box.onpointermove=show;box.onpointerdown=show;box.onpointerleave=hide}
function board(){
  var rows=R.methods.map(function(m){return {m:m,v:m.eq[di]}});rows.sort(function(a,b){return b.v-a.v});
  var B=R.bench&&R.bench.eq,html="",shown=0;
  rows.forEach(function(r,i){var m=r.m,on=CH.indexOf(m.id)>=0;if(!on&&m.id!==focus&&i>=6)return;shown++;
    html+='<a class="row'+(m.id===focus?" on":"")+'" data-id="'+m.id+'"><span class="rk">'+(i+1)+'</span><span class="sw" style="'+
      (on?"background:"+col(m.ci):m.id===focus?"background:currentColor":"")+'"></span><span class="rt"><b>'+m.label+'</b><small>'+
      ""+m.slots+'종목</small></span><span class="rr"><b class="'+(r.v>1?"up":r.v<1?"down":"")+'">'+
      pc((r.v-1)*100)+'</b><small>'+won(R.amount*r.v)+'</small></span></a>'});
  if(B)html+='<a class="row"><span class="rk"></span><span class="sw bench"></span><span class="rt"><b>'+R.bench.name+
    '</b><small>그냥 들고 있기</small></span><span class="rr"><b class="'+(B[di]>1?"up":B[di]<1?"down":"")+'">'+pc((B[di]-1)*100)+
    '</b><small>'+won(R.amount*B[di])+'</small></span></a>';
  var rest=R.methods.length-shown;if(rest>0)html+='<p class="lead" style="margin:6px 0 0">나머지 '+rest+'개 방식은 아래 최종 순위에 있어요.</p>';
  var b=$("simboard");b.innerHTML=html;b.querySelectorAll("a[data-id]").forEach(function(a){a.addEventListener("click",function(){
    focus=a.dataset.id;render()})})}
function feed(){
  var m=R.by[focus];$("simfocus").textContent=m.label+" 매매 기록";var ev=m.events,hold={},last=[];
  for(var i=0;i<ev.length&&ev[i][0]<=di;i++){var e=ev[i];if(e[1]==="b")hold[e[2]]=e;else delete hold[e[2]];last.push(e)}
  var H=Object.keys(hold);$("simhold").innerHTML=H.length?'<div class="hold">'+H.map(function(c){var e=hold[c];
    return "<span><b>"+c+"</b><small>"+(R.strat_ko[e[3]]||e[3])+" · "+(di-e[0])+"일째</small></span>"}).join("")+"</div>"
    :'<p class="lead">지금은 들고 있는 종목이 없어요 (현금).</p>';
  var cur=R.cur==="USD"?"$":"",f=last.slice(-8).reverse().map(function(e){var nm=R.names[e[2]]||"",px=cur+(+e[4]).toLocaleString("en-US",{maximumFractionDigits:2});
    return '<li class="note"><span class="d">'+dt(R.dates[e[0]]).slice(3)+'</span><span class="x">'+(e[1]==="b"?"🟢 <b>"+e[2]+"</b> 매수":
      "⚪ <b>"+e[2]+"</b> 매도 <b class='"+(e[5]>0?"up":"down")+"'>"+pc(e[5])+"</b>")+'<small>'+(nm&&nm!==e[2]?nm+" · ":"")+
      (R.strat_ko[e[3]]||e[3])+" · "+px+(e[1]==="s"?" · "+(WHY[e[6]]||"청산"):"")+'</small></span></li>'}).join("");
  $("simfeed").innerHTML=f||'<li class="note">아직 거래가 없어요.</li>'}
function learn(){
  var q=null;R.learn.forEach(function(x){if(x.di<=di)q=x});var box=$("simlearn");
  if(!q){$("simq").textContent="";$("simlearnlead").textContent="첫 분기 학습 전이에요.";box.innerHTML="";return}
  var y=q.date.slice(0,4),qq=Math.floor((+q.date.slice(5,7)-1)/3)+1;$("simq").textContent=y+"년 "+qq+"분기";
  var used=q.rows.filter(function(r){return r.use}).length;
  $("simlearnlead").textContent=dt(q.date)+"에 그때까지 끝난 거래만 보고 다시 골랐어요. 시장 분위기는 "+(REG[{bull:"u",bear:"d"}[q.regime]||"n"]||REG.n)[0]+
    " → AI 학습기가 쓸 전략 "+used+"개"+(q.best?" · 1등은 "+(R.strat_ko[q.best]||q.best):"")+((q.top3||[]).length?" · 상위 3개: "+q.top3.map(function(n){return R.strat_ko[n]||n}).join(", "):"");
  box.innerHTML=q.rows.map(function(r){return '<div class="lrn'+(r.use?"":" off")+'"><span>'+(r.use?"✅":"⛔")+'</span><span class="rt"><b>'+
    (R.strat_ko[r.s]||r.s)+(r.s===q.best?' <em class="good">1등</em>':"")+'</b><small>'+(r.n?"지난 2년 신호 "+r.n+"번 · 한 번 평균 "+pc(r.exp,2):"아직 기록이 부족해요")+
    '</small></span><span class="rr"><b class="'+(r.now>0?"up":r.now<0?"down":"")+'">'+(r.now==null?"-":pc(r.now,2))+'</b><small>지금 장세 기대</small></span></div>'}).join("");
  var k=R.ml_retrain.filter(function(x){return x<=di}).length;
  $("simml").textContent=R.ml_retrain.length?"🤖 AI 점수 모델은 이 날까지 "+k+"번 다시 학습했어요 (약 3개월마다, 그때까지의 데이터로만).":""}
})();
</script>"""


# ---------------------------------------------------------------- forward mock investing (live.py, kv["live"])
LIVE_ORDER = ["learner", "learner10", "best", "top3", "equal"]  # colour follows the method, never its rank
WHY_KO = {"stop": "손절", "stop_gap": "손절(갭)", "target": "목표가", "target_gap": "목표가(갭)", "rule": "조건 청산",
          "time": "기간 만료"}


def _live_chart(L: dict) -> str:
    ser = [(k, L["methods"][k]) for k in LIVE_ORDER if k in L["methods"]]
    n = len(ser[0][1]["eq"]) if ser else 0
    if n < 3:
        return ""
    data = {"d": [p[0] for p in ser[0][1]["eq"]], "s": [{"k": k, "i": LIVE_ORDER.index(k), "l": m["label"],
                                                        "v": [p[1] for p in m["eq"]]} for k, m in ser]}
    be = L["bench"]["eq"]
    if len(be) == n:
        data["b"] = {"l": L["bench"]["name"], "v": [p[1] for p in be]}
    return (f'<div class="lvchart" id="lvchart" role="img" aria-label="방식별 평가액 변화" '
            f'data-j=\'{h(json.dumps(data, ensure_ascii=False, separators=(",", ":")))}\'></div>')


def live_card(L: dict | None, base: str = "") -> str:
    if not L:
        return ""
    amt, ms = L["amount"], L["methods"]
    days = len(next(iter(ms.values()))["eq"]) - 1
    head = (f'<p class="lead">{_dt(L["start"])}부터 방식마다 <b>가상 {amt // 10_000:,}만원</b>으로 실제처럼 굴려요. '
            '매일 장 마감 후 그날 일만 처리하고, 한 번 기록한 날은 다시 고치지 않아요. ⭐는 Claude가 고른 방식이에요'
            + (" (미국은 원화로 바꿔 넣었다고 치고, 환율 변화는 빼고 계산)" if L.get("market") == "us" else "") + '.</p>')
    if days == 0:
        return card("📒 지금부터 모의투자", head + empty("⏳", "첫 매수는 <b>다음 거래일 시가</b>에 해요. 오늘 장 마감 신호로 골라요."),
                    count=f"{_dt(L['start'])} 시작")
    from . import live as LV
    rows = ""
    for r in LV.summary(L):
        g = (r["eq"] - 1) * 100
        rows += (f'<tr><td><i class="sw lv{LIVE_ORDER.index(r["id"]) if r["id"] in LIVE_ORDER else 0}"></i>'
                 f'{"⭐ " if r["pick"] else ""}<b>{h(r["label"])}</b><small class="sub">보유 {r["n"]}종목 · 산 횟수 {r["trades"]}번</small></td>'
                 f'<td>{money(amt * r["eq"], "KRW")}</td><td class="{tone(g)}">{pct(g, 1)}</td></tr>')
    b = L["bench"]["eq"][-1][1] if L["bench"]["eq"] else None
    if b:
        rows += (f'<tr class="ref"><td><i class="sw ref"></i>📈 {h(L["bench"]["name"])} 그냥 들고 있기</td>'
                 f'<td>{money(amt * b, "KRW")}</td><td class="{tone(b - 1)}">{pct((b - 1) * 100, 1)}</td></tr>')
    body = head + _live_chart(L) + ('<div class="tscroll"><table class="t rank"><thead><tr><th>방식</th><th>지금 평가액</th>'
                                    f'<th>수익률</th></tr></thead><tbody>{rows}</tbody></table></div>')
    P = ms.get(L.get("pick"))
    if P:
        nm = L.get("names", {})
        hold = "".join(
            f'<li class="note"><span class="d">{p["entry_date"][5:].replace("-", "/")}</span><span class="x">'
            f'<b>{h(nm.get(p["code"], p["code"]))}</b> <span class="{tone(p["last"] / p["entry"] - 1)}">'
            f'{pct((p["last"] / p["entry"] - 1) * 100, 1)}</span><small>{h(LB.strat(p["strategy"]))} · 산 가격 {p["entry"]:,.2f} · '
            f'손절 {p["stop"]:,.2f}' + (f' · 목표 {p["target"]:,.2f}' if p.get("target") else "") + '</small></span></li>'
            for p in P["pos"])
        feed = "".join(
            f'<li class="note"><span class="d">{e[0][5:].replace("-", "/")}</span><span class="x">'
            f'{"🟢 샀어요" if e[1] == "b" else "🔴 팔았어요"} <b>{h(nm.get(e[2], e[2]))}</b>'
            + (f' <span class="{tone(e[5])}">{pct(e[5], 1)}</span>' if e[1] == "s" else "")
            + f'<small>{h(LB.strat(e[3]))}' + (f' · {WHY_KO.get(e[6], e[6])}' if e[1] == "s" else "") + '</small></span></li>'
            for e in reversed(P["events"][-10:]))
        body += (f'<h3 class="lvh">⭐ {h(P["label"])} · 지금 들고 있는 것</h3>'
                 + (f'<ul class="plain feed">{hold}</ul>' if hold else '<p class="lead">지금은 현금만 들고 있어요.</p>')
                 + (f'<h3 class="lvh">최근 매매</h3><ul class="plain feed">{feed}</ul>' if feed else ""))
    return card("📒 지금부터 모의투자", body, ("/reports?t=notes", "투자 노트"), f"{days}거래일째")


def live_page(L: dict | None) -> str:
    c = live_card(L)
    return (c + LIVE_CSS + LIVE_JS) if c else ""


def live_home(L: dict | None, base: str = "") -> str:
    if not L:
        return ""
    from . import live as LV
    rows = LV.summary(L)
    pick = next((r for r in rows if r["pick"]), rows[0] if rows else None)
    if not pick:
        return ""
    g = (pick["eq"] - 1) * 100
    return card("📒 지금부터 모의투자",
                f'<a class="row" href="{base}/sim"><span class="av">⭐</span><span class="rt"><b>{h(pick["label"])}</b>'
                f'<small>{_dt(L["start"])}부터 가상 {L["amount"] // 10_000:,}만원 · {len(rows)}개 방식 경쟁 중</small></span>'
                f'<span class="rr"><b>{money(L["amount"] * pick["eq"], "KRW")}</b><small class="{tone(g)}">{pct(g, 1)}</small></span>'
                f'<span class="go"></span></a>', (f"{base}/sim", "보기"))


LIVE_CSS = """<style>
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:7px;vertical-align:0}
.lv0{background:#2a78d6}.lv1{background:#eb6834}.lv2{background:#1baf7a}.lv3{background:#eda100}.lv4{background:#e87ba4}
.sw.ref{background:transparent;border:1.5px dashed var(--ink3)}
@media (prefers-color-scheme:dark){.lv0{background:#3987e5}.lv1{background:#d95926}.lv2{background:#199e70}.lv3{background:#c98500}.lv4{background:#d55181}}
.lvh{font-size:15px;margin:18px 0 2px}.lvchart{position:relative;height:180px;margin:4px 0 10px;touch-action:pan-y}
.lvchart svg{display:block;width:100%;height:100%;overflow:visible}.lvchart .g{stroke:var(--line)}.lvchart .gl{fill:var(--ink3);font-size:11px}
.lvchart .ln{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}.lvchart .bench{stroke:var(--ink3);stroke-dasharray:5 4;stroke-width:1.5}
.lvchart .x{stroke:var(--ink3)}.lvtip{position:absolute;top:0;background:var(--ink);color:var(--card);font-size:12px;padding:6px 9px;border-radius:10px;
pointer-events:none;white-space:nowrap;line-height:1.5;font-variant-numeric:tabular-nums}.lvtip i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px}
</style>"""

LIVE_JS = r"""<script>
(function(){var el=document.getElementById("lvchart");if(!el)return;var D=JSON.parse(el.dataset.j),NS="http://www.w3.org/2000/svg";
var L=["#2a78d6","#eb6834","#1baf7a","#eda100","#e87ba4"],K=["#3987e5","#d95926","#199e70","#c98500","#d55181"],dk=matchMedia("(prefers-color-scheme: dark)");
function col(i){return (dk.matches?K:L)[i]}
function draw(){el.innerHTML="";var W=el.clientWidth,H=el.clientHeight,pl=40,pr=8,pt=8,pb=20,n=D.d.length,all=[];
D.s.forEach(function(s){all=all.concat(s.v)});if(D.b)all=all.concat(D.b.v);var lo=Math.min.apply(0,all),hi=Math.max.apply(0,all);
if(hi-lo<0.01){lo-=0.005;hi+=0.005}var X=function(i){return pl+(W-pl-pr)*i/(n-1)},Y=function(v){return pt+(H-pt-pb)*(hi-v)/(hi-lo)};
var svg=document.createElementNS(NS,"svg");function E(t,a){var e=document.createElementNS(NS,t);for(var k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e}
for(var g=0;g<=3;g++){var v=lo+(hi-lo)*g/3,y=Y(v);E("line",{x1:pl,x2:W-pr,y1:y,y2:y,"class":"g"});
E("text",{x:pl-6,y:y+4,"text-anchor":"end","class":"gl"}).textContent=((v-1)*100>0?"+":"")+((v-1)*100).toFixed(1)+"%"}
E("text",{x:pl,y:H-4,"class":"gl"}).textContent=D.d[0].slice(5).replace("-","/");
E("text",{x:W-pr,y:H-4,"text-anchor":"end","class":"gl"}).textContent=D.d[n-1].slice(5).replace("-","/");
function path(v){return v.map(function(x,i){return(i?"L":"M")+X(i).toFixed(1)+" "+Y(x).toFixed(1)}).join("")}
if(D.b)E("path",{d:path(D.b.v),"class":"ln bench"});D.s.forEach(function(s){E("path",{d:path(s.v),"class":"ln",stroke:col(s.i)})});
var cx=E("line",{y1:pt,y2:H-pb,"class":"x",visibility:"hidden"});el.appendChild(svg);var tip=document.createElement("div");tip.className="lvtip";tip.style.display="none";el.appendChild(tip);
function mv(ev){var r=el.getBoundingClientRect(),i=Math.round((ev.clientX-r.left-pl)/(W-pl-pr)*(n-1));if(i<0||i>=n){out();return}
cx.setAttribute("x1",X(i));cx.setAttribute("x2",X(i));cx.setAttribute("visibility","visible");
var rows=D.s.map(function(s){return [s.v[i],'<i style="background:'+col(s.i)+'"></i>'+s.l]});if(D.b)rows.push([D.b.v[i],'<i style="border:1px dashed currentColor"></i>'+D.b.l]);
rows.sort(function(a,b){return b[0]-a[0]});tip.innerHTML="<b>"+D.d[i]+"</b><br>"+rows.map(function(r){var p=(r[0]-1)*100;return r[1]+" "+(p>0?"+":"")+p.toFixed(1)+"%"}).join("<br>");
tip.style.display="block";var tx=X(i)+12;if(tx+tip.offsetWidth>W)tx=X(i)-12-tip.offsetWidth;tip.style.left=Math.max(0,tx)+"px"}
function out(){cx.setAttribute("visibility","hidden");tip.style.display="none"}
el.onpointermove=mv;el.onpointerdown=mv;el.onpointerleave=out}
draw();addEventListener("resize",draw);dk.addEventListener&&dk.addEventListener("change",draw)})();
</script>"""


# ---------------------------------------------------------------- 투자 노트 (/reports?t=notes)
CONF_KO = {"high": ("믿을 만함", "good"), "mid": ("어느 정도", ""), "low": ("아직 이름", "bad")}


def notes_page(L: dict | None, pb: dict | None, backtests: dict | None = None) -> str:
    from . import live as LV
    S = LV.stats(L, backtests) if L else None
    if not S:
        return card("📓 투자 노트", empty("📓", "지금부터 모의투자가 시작되면 여기에 배운 것이 쌓여요."))
    out = []
    ready = LV.readiness(S, L.get("pick", LV.PICK))
    ok = sum(r["ok"] for r in ready)
    pick = next((m for m in S["methods"] if m["pick"]), None)
    lst = "".join(f'<li class="note"><span class="ck {"on" if r["ok"] else ""}">{"✓" if r["ok"] else ""}</span>'
                  f'<span class="x">{h(r["label"])}<small>지금 {h(r["now"])}</small></span></li>' for r in ready)
    out.append(card(f"직접 투자 준비도 {ok}/{len(ready)}",
                    f'<p class="lead">⭐ {h(pick["label"] if pick else "")} 기준이에요. 다섯 개가 다 채워지기 전에는 큰돈을 넣지 않는 걸 권해요. '
                    f'채워져도 처음엔 작게 시작하세요.</p><ul class="plain feed ready">{lst}</ul>'))
    note = (pb or {}).get("note")
    if note:
        def items(xs):
            return "".join(
                f'<li class="note"><span class="x"><b>{h(x["title"])}</b> <em class="{CONF_KO.get(x["confidence"], ("", ""))[1]}">'
                f'{CONF_KO.get(x["confidence"], (x["confidence"], ""))[0]}</em><small>{h(x["detail"])}</small>'
                f'<small>근거: {h(x["evidence"])} · {x["n"]}건</small></span></li>' for x in xs)
        body = f'<p class="lead"><b>{h(note["headline"])}</b></p>'
        if note.get("my_rules"):
            body += '<h3 class="lvh">내가 직접 투자할 때 지킬 것</h3><ol class="rules">' + "".join(
                f'<li><b>{h(r["rule"])}</b><small>{h(r["why"])}</small></li>' for r in note["my_rules"]) + '</ol>'
        if note.get("works"):
            body += f'<h3 class="lvh">👍 잘 된 것</h3><ul class="plain feed">{items(note["works"])}</ul>'
        if note.get("fails"):
            body += f'<h3 class="lvh">👎 안 된 것</h3><ul class="plain feed">{items(note["fails"])}</ul>'
        if note.get("watch"):
            body += '<h3 class="lvh">👀 다음 주에 볼 것</h3><ul class="plain">' + "".join(f'<li class="note">{h(w)}</li>' for w in note["watch"]) + '</ul>'
        if note.get("changes"):
            body += '<h3 class="lvh">지난 노트에서 바뀐 것</h3><ul class="plain">' + "".join(f'<li class="note">{h(c)}</li>' for c in note["changes"]) + '</ul>'
        ts = (pb or {}).get("note_ts") or ""
        out.append(card("📓 투자 노트", body, count=f"{ts[5:7]}/{ts[8:10]} 업데이트" if ts else ""))
    else:
        out.append(card("📓 투자 노트", empty("🗓️", "첫 노트는 토요일 주간 학습 때 Claude가 써요. 매주 모의투자 결과를 보고 고쳐 써요.")))
    if S["strategies"]:
        rows = "".join(
            f'<tr><td><b>{h(LB.strat(x["name"]))}</b><small class="sub">평균 {x["hold_days"]:.0f}일 보유 · '
            + h(", ".join(f"{WHY_KO.get(k, k)} {v}" for k, v in x["exits"].items())) + '</small></td>'
            f'<td>{x["n"]}</td><td>{pct(x["win_rate"], 0, False)}</td><td class="{tone(x["avg_pct"])}">{pct(x["avg_pct"], 2)}</td>'
            f'<td>{pct(x["backtest_avg_pct"], 2) if x["backtest_avg_pct"] is not None else "-"}</td></tr>' for x in S["strategies"])
        out.append(card("전략별 실전 성적", '<p class="lead">지금부터 모의투자에서 끝난 거래만 셌어요 (같은 거래는 한 번만). '
                        '과거 백테스트 평균과 비슷하면 그 전략을 믿을 근거가 늘어나요.</p><div class="tscroll"><table class="t rank">'
                        '<thead><tr><th>전략</th><th>거래</th><th>이긴 비율</th><th>평균</th><th>과거 평균</th></tr></thead>'
                        f'<tbody>{rows}</tbody></table></div>'))
    else:
        out.append(card("전략별 실전 성적", empty("⏳", "아직 끝난 거래가 없어요. 첫 매도가 나오면 여기에 쌓여요.")))
    return "".join(out) + NOTES_CSS


NOTES_CSS = """<style>
.ready .ck{flex:0 0 22px;height:22px;border-radius:50%;border:2px solid var(--line);display:inline-flex;align-items:center;justify-content:center;
font-size:13px;font-weight:700;color:#fff}.ready .ck.on{background:var(--accent);border-color:var(--accent)}
ol.rules{margin:4px 0 0;padding-left:22px}ol.rules li{margin:8px 0}ol.rules small{display:block;color:var(--ink3);font-size:12.5px}
.lvh{font-size:15px;margin:18px 0 2px}
</style>"""
