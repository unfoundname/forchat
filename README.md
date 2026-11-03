# ForChat

涓€涓?workflow 鍘熻寮曟搸锛?*鍘熻锛坧rimitives锛夆啋 鍙姞杞界殑 workflow 閰嶇疆 鈫?涓€娆¤繍琛?*銆?
涓嶅惈浠讳綍涓氬姟锛?澶嶅埢鏌愭煇"鏄繍琛岃緭鍏ワ紝涓嶆槸浠ｇ爜銆?

## 鐩綍

```
ForChat/
鈹溾攢鈹€ AGENTS.md                   缁?agent 鐨勫伐浣滆鍒欙紙鍥涙潯纭鍒欙級
鈹溾攢鈹€ CONTEXT.md                  棰嗗煙璇嶆眹琛細涓夊眰銆亀orker銆乫ence銆乺eport鈥?
鈹溾攢鈹€ docs/decisions/             ADR
鈹溾攢鈹€ forchat/
鈹?  鈹溾攢鈹€ __main__.py             CLI锛氬彧瑁呴厤锛屼笉鎵ц
鈹?  鈹溾攢鈹€ primitives/             鈽?鍘熻灞傦紙鍞竴鎵ц寰幆鍦ㄨ繖閲岋級
鈹?  鈹?  鈹溾攢鈹€ types.py            WorkerInstance / Budget / Report
鈹?  鈹?  鈹溾攢鈹€ store.py            EventLog锛歛ppend-only 浜嬩欢娴?
鈹?  鈹?  鈹溾攢鈹€ scheduler.py        骞跺彂鍑嗗叆锛堢瓑寰呭瀷瀛?worker 鍐呰仈锛岄伩鍏嶆閿侊級
鈹?  鈹?  鈹溾攢鈹€ hooks.py            action / spawn 涓ょ被 hook
鈹?  鈹?  鈹溾攢鈹€ workflow.py         workflow.json 涓ユ牸鍔犺浇涓庢牎楠?
鈹?  鈹?  鈹溾攢鈹€ runtime.py          鈽?Runtime锛歴pawn / report / read_state / write_note
鈹?  鈹?  鈹斺攢鈹€ replay.py           review锛堜汉璇伙級/ tree / tool_calls
鈹?  鈹溾攢鈹€ providers/              base(Protocol) 路 fake(绂荤嚎) 路 deepseek(鍞竴鐢?SDK)
鈹?  鈹溾攢鈹€ tools/                  鑳藉姏宸ュ叿锛歳ead / list_dir / run_python / skill
鈹?  鈹溾攢鈹€ skills/                 鎶€鑳藉姞杞斤紙SKILL.md锛?
鈹?  鈹溾攢鈹€ mcp.py                  MCP 瀹㈡埛绔紙stdio + JSON-RPC锛?
鈹?  鈹斺攢鈹€ mcp_bridge.py           MCP 宸ュ叿 鈫?宸ュ叿娉ㄥ唽琛?
鈹溾攢鈹€ examples/
鈹?  鈹溾攢鈹€ workflows/minimal/      鏈€灏?workflow 鍖咃紙workflow.json + prompts/锛?
鈹?  鈹溾攢鈹€ run_demo.py             绔埌绔嚜妫€锛堝亣 provider锛屾棤闇€ key锛?
鈹?  鈹溾攢鈹€ mcp_demo_server.py      80 琛岀殑 MCP 鏈嶅姟绔?
鈹?  鈹斺攢鈹€ smoke_mcp.py            MCP 閾捐矾鑷
鈹溾攢鈹€ tests/
鈹?  鈹溾攢鈹€ test_primitives.py      鍘熻鑷锛? 鏉★級
鈹?  鈹斺攢鈹€ test_layering.py        渚濊禆鏂瑰悜 / 棰嗗煙璇?/ 鍞竴鎵ц寰幆
鈹溾攢鈹€ skills/                     鎶€鑳界洰褰曪紙鏁版嵁锛?
鈹斺攢鈹€ mcp_servers.json            MCP 鏈嶅姟绔厤缃?
```

## 璺戣捣鏉ワ紙涓嶉渶瑕?key锛?

```powershell
python examples/run_demo.py        # 鍋?provider 璺戜竴娆″畬鏁磋繍琛岋細root 鈫?coder 鈫?hook 娲剧敓 reviewer
python tests/test_primitives.py    # 鍘熻鑷
python tests/test_layering.py      # 鏋舵瀯绾︽潫
```

## 璺戠湡妯″瀷

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxx"
python -m forchat "鎶婃渶灏忕郴缁熻窇閫?
python -m forchat --workflow examples/workflows/minimal/workflow.json "鐩爣"
python -m forchat --tools          # 鑳藉姏宸ュ叿锛堝惈 MCP锛?
python -m forchat --skills         # 鎶€鑳?
python -m forchat --mcp            # 杩?MCP 鏈嶅姟绔?
```

## 涓夊眰

| 灞?| 鏄粈涔?| 鏀惧摢 |
|---|---|---|
| 鍘熻 | 鎵ц妯″瀷 / 鑳藉姏闈?/ 璁板綍闈?/ 瑁呴厤闈?| `forchat/primitives/` |
| workflow | 鍙姞杞介厤缃細worker 妯℃澘銆乧an_spawn銆侀绠椼€乭ook銆侀檺鍒?| `examples/workflows/<name>/workflow.json` |
| 杩愯杈撳叆 | 鐢ㄦ埛鐩爣涓庢緞娓呯粨鏋滐紙spec/plan锛?| 杩愯鏃剁姸鎬侊紝涓嶈繘浠撳簱 |

## 鍐欎竴涓?workflow 鍖?

```
examples/workflows/minimal/
鈹溾攢鈹€ workflow.json
鈹斺攢鈹€ prompts/{root,coder,reviewer}.md
```

`workflow.json` 鍙弿杩扮粨鏋勶紝瀛楁鎷奸敊浼氬湪鍔犺浇鏈熸姤閿欙細

```json
{
  "name": "minimal", "version": 1, "entry": "root",
  "limits": {"max_concurrent": 2, "max_depth": 3, "max_workers": 8},
  "workers": {
    "root": {
      "role_file": "prompts/root.md",
      "can_spawn": ["coder"],
      "tools": ["spawn", "report", "read_state", "write_note"],
      "budget": {"tokens": 20000, "wall_seconds": 600},
      "hooks": {"on_done": [{"action": "snapshot"}]}
    }
  }
}
```

杩愯鏃跺彧缁欐ā鍨嬪洓涓伐鍏凤細`spawn`锛堟淳瀛?worker锛屽彈 can_spawn/娣卞害/worker 鏁伴噺绾︽潫锛夈€乣report`锛堝畬鎴愪俊鍙凤級銆乣read_state` / `write_note`锛坧lan 涓庣瑪璁帮紝闃叉涓婁笅鏂囬仐蹇橈級銆?

## 鑳藉姏宸ュ叿锛坮ead / list_dir / run_python / skill锛?

鍦?`forchat/tools/` 鏂板缓鏂囦欢鍗宠嚜鍔ㄦ敞鍐岋紝schema 鐢辩鍚嶆帹瀵硷細

```python
from ..registry import tool

@tool(description="鎶婃枃鏈浆鎴愬ぇ鍐?, params={"text": "瑕佽浆鎹㈢殑鏂囨湰"})
def upper(text: str) -> str:
    return text.upper()
```

> 鎺ュ叆鏂瑰紡锛歚forchat/tool_bridge.py` 鎶?registry 閲岀殑宸ュ叿瀵煎嚭鎴?`{name: (spec, caller)}` 娉ㄥ叆 `Runtime`銆?
> 鍦?`workflow.json` 鐨?`tools` 閲屽啓宸ュ叿鍚嶅嵆鍙紙鏍￠獙鏃朵細鎷?杩欏彴鏈哄櫒涓婂綋鍓嶆敞鍐屼簡鍝簺宸ュ叿"鏉ユ瘮瀵癸級锛?

## 鎶€鑳?

`skills/<鍚嶅瓧>/SKILL.md`锛宖rontmatter 鍙湁 `name` / `description`锛屾鏂囨槸瀹屾暣鎸囦护銆?

鍦?workflow 閲屾寕杞斤細`"skills": ["code-graph"]`鈥斺€攚orker 鍒濆鍖栨椂姝ｆ枃鎷艰繘瀹冪殑绯荤粺鎻愮ず銆?
鍔犺浇鍣ㄧ敱 `tool_bridge.build_skill_loader()` 娉ㄥ叆锛屽師璇眰鍙嬁鍒?`name -> body` 鐨勫嚱鏁帮紝
涓嶈璇?SKILL.md 鐨勬牸寮忋€傛妧鑳藉彲浠ュ甫鑴氭湰锛岀敤 `run_python` 璺戯紝瑙?`skills/code-graph/`銆?

## MCP

`mcp_servers.json` 澹版槑鏈嶅姟绔紱宸ュ叿鍚嶅甫 `mcp__<server>__<tool>` 鍓嶇紑锛岃繙绔伐鍏疯蛋鍘熷鍙傛暟 JSON锛坄raw_caller`锛夛紝鍥犱负杩滅 schema 鍙兘宓屽銆傚彧鏀寔 stdio + tools 鑳藉姏銆?

## 瀛︿範璺緞

1. `CONTEXT.md` 鈥斺€?鍏堝榻愯瘝姹囷紝涓夊眰涓嶈娣?
2. `forchat/primitives/types.py` 鈥斺€?WorkerInstance / Budget / Report
3. `forchat/primitives/runtime.py` 鈥斺€?鍞竴鎵ц寰幆锛歴pawn / hook / report
4. `forchat/primitives/workflow.py` 鈥斺€?閰嶇疆濡備綍琚弗鏍兼牎楠?
5. `forchat/primitives/replay.py` 鈥斺€?浠庝簨浠舵祦閲嶅缓涓€娆¤繍琛?
6. `examples/workflows/minimal/` 鈥斺€?閰嶇疆鎬庝箞鎷煎嚭缁撴瀯
