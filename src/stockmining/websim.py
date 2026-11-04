"""Dashboard page /sim: time-machine mock investing (data from replay.py, kv["replay"]).

The ranking and notes are rendered on the server; the replay player (racing equity chart, live leaderboard,
learning log, trade feed, holdings) runs in the browser from /api/sim.
"""
from __future__ import annotations

import json

from . import labels as LB
from .webui import card, empty, h, money, pct, tone

CHART_N = 5  # methods drawn on the chart (+ benchmark)
TAG = {"system": '<em class="good">미래 모름</em>', "single": '<em>지나고 보니</em>', "fixed": '<em class="warn">지나고 보니</em>'}


def load(db) -> dict | None:
    raw = db.get("replay")
    return json.loads(raw) if raw else None


def _dt(s: str) -> str:
    return f"{s[:4]}.{s[5:7]}.{s[8:10]}"


def charted(R: dict) -> list[str]:
    ids = [m["id"] for m in R["methods"][:CHART_N - 1]]
    if "learner" not in ids and any(m["id"] == "learner" for m in R["methods"]):
        ids.append("learner")
    else:
        ids = [m["id"] for m in R["methods"][:CHART_N]]
    return ids


def page_body(R: dict | None) -> str:
    if not R:
        return card("타임머신 모의투자", empty("⏳", "아직 계산 전이에요. 서버에서 <code>stockmining replay</code> 가 끝나면 나타나요."))
    ms, amt = R["methods"], R["amount"]
    top = ms[0]
    learner = next((m for m in ms if m["id"] == "learner"), None)
    best_sys = next((m for m in ms if m["kind"] == "system"), top)
    b = R.get("bench") or {}
    bt = (b.get("metrics") or {}).get("total_pct")
    sub = ""
    if top is not best_sys:
        sub = (f'<p class="lead" style="margin-top:8px">지나고 보니 1등은 <b>{h(top["label"])}</b> '
               f'{pct(top["metrics"]["total_pct"], 1)}였지만, 그건 미리 알 수 없던 선택이에요.</p>')
    out = [f'<section class="hero"><small>{_dt(R["start"])} ~ {_dt(R["end"])} · 가상 {amt // 10_000:,}만원으로 시작</small>'
           f'<span class="lbl">미래를 모르고도 가장 많이 번 방식</span><b class="big" style="font-size:28px">{h(best_sys["label"])}</b>'
           f'<p><span class="{tone(best_sys["metrics"]["total_pct"])}">{pct(best_sys["metrics"]["total_pct"], 1)}</span>'
           f' · {money(amt * (1 + best_sys["metrics"]["total_pct"] / 100), "KRW")}'
           + (f' · {h(b["name"])} {pct(bt, 1)}' if bt is not None else "") + '</p>'
           f'<p class="desc">과거로 돌아가 하루씩 앞으로 가며 투자해 봤어요. 매일 <b>그날 장 마감까지 알 수 있던 것만</b> 보고 '
           f'다음 날 사고팔았고, 학습하는 방식은 분기마다 <b>이미 끝난 거래만</b> 보고 전략을 다시 골랐어요.</p>{sub}</section>']

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

    # final ranking (static)
    rows = ""
    for i, m in enumerate(ms, 1):
        x = m["metrics"]
        link = f'/strategy/{h(m["strategy"])}' if m.get("strategy") else None
        name = (f'<a href="{link}">{h(m["label"])}</a>' if link else f'<b>{h(m["label"])}</b>')
        tag = TAG.get(m["kind"], "")
        rows += (f'<tr><td>{i}. {name} {tag}<small class="sub">{m["slots"]}종목 · 거래 {x["trades"]}번'
                 f'{" · 이긴 비율 " + pct(x["win_rate"], 0, False) if x["win_rate"] is not None else ""}</small></td>'
                 f'<td class="{tone(x["total_pct"])}">{pct(x["total_pct"], 1)}</td><td>{pct(x["cagr_pct"], 1)}</td>'
                 f'<td class="down">-{x["mdd_pct"]:.0f}%</td></tr>')
    b = R.get("bench") or {}
    if b.get("metrics"):
        rows += (f'<tr class="ref"><td>📈 {h(b["name"])} 그냥 들고 있기<small class="sub">비교 기준</small></td>'
                 f'<td class="{tone(b["metrics"]["total_pct"])}">{pct(b["metrics"]["total_pct"], 1)}</td><td>-</td>'
                 f'<td class="down">-{b["metrics"]["mdd_pct"]:.0f}%</td></tr>')
    rnd = R.get("random")
    if rnd:
        rows += (f'<tr class="ref"><td>🎲 아무거나 골랐을 때<small class="sub">같은 신호를 무작위로 {rnd["runs"]}번 · 중앙값 '
                 f'(10~90%: {pct(rnd["p10_pct"], 0)} ~ {pct(rnd["p90_pct"], 0)})</small></td>'
                 f'<td class="{tone(rnd["median_pct"])}">{pct(rnd["median_pct"], 1)}</td><td>-</td><td>-</td></tr>')
    out.append(card("최종 순위", '<p class="lead">총수익 순서예요. 최대 낙폭은 고점에서 가장 많이 빠졌던 정도예요.</p>'
                    '<div class="tscroll"><table class="t rank"><thead><tr><th>방식</th><th>총수익</th><th>1년 환산</th>'
                    f'<th>최대 낙폭</th></tr></thead><tbody>{rows}</tbody></table></div>', count=f"{len(ms)}개"))

    notes = ["<b>'미래 모름'</b> 방식은 그때 알 수 있던 정보만으로 전략을 골라서, 실제로 했어도 같은 결과가 나와요. "
             "<b>'지나고 보니'</b>는 어떤 전략이 잘될지 미리 안다고 치고 계산한 거라 그대로 믿으면 안 돼요."]
    if bt is not None:
        for m in [x for x in (best_sys, learner) if x]:
            d = m["metrics"]["total_pct"] - bt
            notes.append(f"{h(m['label'])}: {h(b['name'])}을 그냥 들고 있었을 때({pct(bt, 1)})보다 "
                         f"{abs(d):.0f}%p {'더 벌었어요' if d > 0 else '덜 벌었어요'}. 대신 최대 낙폭은 "
                         f"{m['metrics']['mdd_pct']:.0f}% (지수 {b['metrics']['mdd_pct']:.0f}%)였어요.")
    rnd = R.get("random")
    if rnd and learner:
        diff = learner["metrics"]["total_pct"] - rnd["median_pct"]
        notes.append(f"AI 학습기는 같은 신호를 아무거나 고른 경우보다 {abs(diff):.1f}%p {'더 벌었어요' if diff > 0 else '덜 벌었어요'}. "
                     + ("다만 차이가 작아서 실력이라고 하기엔 아직 일러요." if 0 < diff < 15 else
                        "고르는 실력이 운보다 낫다는 뜻이에요." if diff > 0 else "아직은 고르는 실력이 운과 구분이 안 돼요."))
    best = next((m for m in ms if m["id"] == "best"), None)
    twin = best and next((m for m in ms if m["kind"] == "single" and m["eq"] == best["eq"]), None)
    if twin:
        notes.append(f"분기 1등 따라가기는 이 기간 내내 <b>{h(twin['label'])}</b>을 1등으로 골라서 두 결과가 똑같아요.")
    wins = sorted((e for e in top["events"] if e[1] == "s" and e[5] is not None), key=lambda e: -e[5])[:3]
    if wins:
        notes.append(f"1등 {h(top['label'])}은 거래 {top['metrics']['trades']}번 중 몇 번의 대박이 대부분을 벌었어요: "
                     + ", ".join(f"{h(e[2])} {pct(e[5], 0)}" for e in wins) + ". 이런 종목을 다시 만난다는 보장은 없어요.")
    notes += ["거래 비용(수수료·슬리피지)은 넣어서 계산했어요. 세금과 환율 변화는 넣지 않았어요.",
              "전략 규칙의 숫자(파라미터)는 전체 기간을 보고 정한 거라 약간은 미래를 아는 셈이에요. 실제로는 이보다 덜 벌 가능성이 커요.",
              "지금 상장된 종목 위주의 데이터라 상장폐지된 종목이 빠져 있어요(생존 편향). 이것도 성적을 부풀려요."]
    out.append(card("꼭 알아두세요", '<ul class="plain">' + "".join(f'<li class="note">{n}</li>' for n in notes) + "</ul>"))
    out.append(f'<p class="updated">{h(R["ts"][:16].replace("T", " "))} (뉴욕 시간) 계산 · 매주 학습 때 다시 계산해요 · '
               f'계산에 {R.get("secs", 0) // 60}분 걸림</p>')
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
fetch("/api/sim",{credentials:"same-origin"}).then(function(r){return r.json()}).then(init).catch(function(){
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
      ({system:"미래 모름 · ",single:"지나고 보니 · ",fixed:"지나고 보니 · "}[m.kind]||"")+m.slots+'종목</small></span><span class="rr"><b class="'+(r.v>1?"up":r.v<1?"down":"")+'">'+
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
    " → AI 학습기가 쓸 전략 "+used+"개"+(q.best?" · 1등 따라가기는 "+(R.strat_ko[q.best]||q.best):"");
  box.innerHTML=q.rows.map(function(r){return '<div class="lrn'+(r.use?"":" off")+'"><span>'+(r.use?"✅":"⛔")+'</span><span class="rt"><b>'+
    (R.strat_ko[r.s]||r.s)+(r.s===q.best?' <em class="good">1등</em>':"")+'</b><small>'+(r.n?"지난 2년 신호 "+r.n+"번 · 한 번 평균 "+pc(r.exp,2):"아직 기록이 부족해요")+
    '</small></span><span class="rr"><b class="'+(r.now>0?"up":r.now<0?"down":"")+'">'+(r.now==null?"-":pc(r.now,2))+'</b><small>지금 장세 기대</small></span></div>'}).join("");
  var k=R.ml_retrain.filter(function(x){return x<=di}).length;
  $("simml").textContent=R.ml_retrain.length?"🤖 AI 점수 모델은 이 날까지 "+k+"번 다시 학습했어요 (약 3개월마다, 그때까지의 데이터로만).":""}
})();
</script>"""
