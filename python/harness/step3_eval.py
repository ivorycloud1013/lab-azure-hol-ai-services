#!/usr/bin/env python3
"""step 3 — 평가 레이어. 방금 한 변경이 실수였다고 말해주는 것.

step 2 까지로 하네스는 다 지어졌다. tool 로 찾고, 검증으로 되짚고, 틀렸으면 다시 찾는다.
그런데 여기서 진짜 문제가 시작된다 — **이제 이걸 고칠 수 있는가.**

검증 문구 하나, tool description 한 줄, 재시도 횟수 하나. 전부 "당연히 좋아지겠지" 싶어서
손대는 것들이고, 실제로는 하나를 고치면서 둘을 조용히 망가뜨린다. 그게 이상한 일이 아니라
하네스 변경의 정상적인 결과다. 알아채지 못하는 것이 문제일 뿐이다.

    python step3_eval.py --endpoint ... --out runs/a.json      # 기록
    # 무언가 바꾼다 — 검증 지시문, tool description, 에러 문구
    python step3_eval.py --endpoint ... --baseline runs/a.json # 대조

기록하는 것이 hit 만이 아니라는 점이 중요하다. 첫 답이 맞았는지, 하네스가 몇 번 반려했는지,
맞는 답을 붙잡은 적은 없는지까지 남긴다. 검증을 조이면 hit 은 그대로인데 헛수고만 늘어나는
변경이 아주 흔하고, hit 만 보는 리포트는 그걸 "변화 없음" 이라고 말한다.

기본 judge 는 none 이고, 그래도 리포트 대부분이 채워진다. hit · token · round · tool error 는
전부 결정론적이다. --judge evaluation 을 주면 azure-ai-evaluation 의 채점 점수가 붙고 실행이
Foundry Evaluation 탭에 올라간다. 행마다 실제 비용이 들어서 기본값이 아니다.
"""

import json
import os
import tempfile
import time

import golden
import harness_cli
import harness_metrics as metrics
import step1_tools
import step2_verify

DOES = ("step 2 까지 지은 하네스 — tool · 검증 · 재시도 — 를 그대로 써서 질문 세트를 한 바퀴 "
        "돌고, 결과를 파일로 남깁니다. --baseline 으로 앞선 실행을 주면 그 뒤로 무엇이 "
        "나빠졌는지 질문 이름으로 알려줍니다.")

WATCH = ("한 번 돌려 --out 으로 기록하고, harness_verify.py 의 판정 지시문이나 "
         "step1_tools.py 의 tool description 을 한 줄 바꿔 보세요. 그다음 --baseline 으로 "
         "다시 돌리면, 분명 나아질 거라 여겼던 그 변경이 실제로 무엇을 했는지 나옵니다. "
         "hit 이 그대로여도 헛수고가 늘었으면 그건 나빠진 것입니다.")

# azure-ai-evaluation 의 기본값은 2024-02-15-preview 인데 요즘 배포는 그걸 거부한다.
JUDGE_API_VERSION = "2024-12-01-preview"
EVALUATOR_THRESHOLD = 3


def account_endpoint(url):
    """project 경로를 떼고 계정 루트만 남긴다.

    --endpoint 하나가 서로 다른 것을 원하는 두 곳을 감당해야 한다. evaluate() 는 project 로
    업로드하고, evaluator 는 고전 AzureOpenAI 경로로 judge 에 닿는데 그쪽은 계정을 원한다.
    하나에서 다른 하나를 뽑아내면 인자는 여전히 하나로 유지된다.
    """
    marker = "/api/projects/"
    base = url.split(marker)[0] if marker in url else url
    return base.rstrip("/")


def answer_all(ctx, verifying):
    """완성된 하네스로 질문 세트를 돌고, 질문마다 기록 하나를 남긴다.

    step 2 의 solve() 를 그대로 부른다. 평가가 재구현한 하네스를 재면 그 평가는 하네스에
    대해 아무것도 말하지 않는다 — 다음 주에 둘이 어긋나고, 어긋난 줄도 모른다.
    """
    records = []
    total = len(ctx["golden"])
    for index, item in enumerate(ctx["golden"], start=1):
        metrics.case(index, total, item["question"])
        attempts = step2_verify.solve(ctx, item, verifying)
        final = attempts[-1]
        text = final["text"]
        calls = [call for attempt in attempts for call in attempt["calls"]]
        hit = final["hit"]
        rejected = sum(1 for a in attempts
                       if a["verdict"] is not None and not a["verdict"].ok)
        metrics.judged(hit, golden.missing_keys(item, text))
        records.append({
            "attempts": len(attempts),
            "first_hit": attempts[0]["hit"],
            "rejected": rejected,
            # 맞는 답을 반려한 횟수. hit 이 그대로인데 이 값만 오르는 변경을 잡으려고 남긴다.
            "wasted": sum(1 for a in attempts if a["hit"] and a["verdict"] is not None
                          and not a["verdict"].ok),
            "id": item["id"],
            "query": item["question"],
            "response": text,
            "context": item["context"],
            "ground_truth": " / ".join(item["answer_key"]),
            "tool_calls": [{"type": "tool_call", "name": c["name"], "arguments": c["arguments"]}
                           for c in calls],
            "tool_definitions": step1_tools.TOOLS,
            "hit": hit,
            "calls": len(calls),
        })
    return records


def compare(records, baseline_path):
    """무엇이 달라졌는지 질문 단위로 이름을 댄다.

    5/6 에서 5/6 으로 그대로인 통과율이 실은 회귀 둘에 수정 둘일 수 있다. 합계만 보고하는 것이
    대시보드는 초록인데 하네스는 썩어가는 방식이다.

    hit 말고 헛수고도 함께 본다. 검증을 조이면 hit 은 그대로면서 맞는 답을 붙잡는 횟수만
    오르는데, 그건 사용자 눈에 "느려지고 답이 뭉개진" 것으로 보이면서 리포트에는 안 잡힌다.
    """
    try:
        with open(baseline_path, encoding="utf-8") as handle:
            baseline = json.load(handle)
    except (OSError, ValueError) as error:
        raise SystemExit(f"baseline {baseline_path} 를 읽을 수 없습니다: {error}")

    was = {row["id"]: row for row in baseline.get("records", [])}
    regressions, fixes, drift = [], [], []
    for record in records:
        old = was.get(record["id"])
        if old is None:
            continue
        if old["hit"] and not record["hit"]:
            regressions.append(record["id"])
        elif not old["hit"] and record["hit"]:
            fixes.append(record["id"])
        if record["calls"] > old["calls"]:
            drift.append(f"{record['id']} {old['calls']}->{record['calls']} 호출")
        # 예전 기록에는 없는 열이라 기본값을 둔다. 기록 형식이 바뀌었다고 대조가 죽으면,
        # 정작 대조가 필요한 순간(= 무언가를 바꾼 직후)에만 못 쓰게 된다.
        if record["wasted"] > old.get("wasted", 0):
            drift.append(f"{record['id']} 헛수고 {old.get('wasted', 0)}->{record['wasted']}번")
    return regressions, fixes, drift


def import_evaluation():
    """azure-ai-evaluation 을 import 하거나, None 을 돌려주고 그냥 계속 간다.

    이게 돌아갈 시점이면 리포트의 결정론적인 절반은 이미 기록돼 있고, 회귀 검사를 지고 있는
    것도 그 절반이다. 전이 의존성의 버전 어긋남은 채점 점수를 앗아가야지 실행을 앗아가면 안 된다.
    """
    try:
        from azure.ai.evaluation import (  # noqa: PLC0415
            GroundednessEvaluator,
            IntentResolutionEvaluator,
            RelevanceEvaluator,
            ToolCallAccuracyEvaluator,
            evaluate,
        )
    except Exception as error:  # noqa: BLE001 — import 실패는 judge 만 낮추고 실행은 살린다
        print(f"  [azure-ai-evaluation 을 쓸 수 없어 채점 점수를 건너뜁니다: {error}]")
        return None
    return {"evaluate": evaluate, "classes": {
        "groundedness": GroundednessEvaluator,
        "relevance": RelevanceEvaluator,
        "intent_resolution": IntentResolutionEvaluator,
        "tool_call_accuracy": ToolCallAccuracyEvaluator,
    }}


def pick_metric(results, evaluator):
    """라이브러리가 그 점수를 뭐라고 불렀는지 못박지 않고 읽는다.

    보통은 "groundedness.groundedness" 인데 접미사가 릴리스마다 움직였고, evaluator 마다
    _threshold 같은 짝꿍 키도 함께 낸다. 접두사로 맞추면 멀쩡한 점수가 n/a 로 보고되는 일이
    없다. 여기서 그게 최악의 실패다 — judge 가 돌았는데 아무것도 못 찾은 것처럼 보인다.
    """
    exact = results.get(f"{evaluator}.{evaluator}")
    if isinstance(exact, (int, float)) and not isinstance(exact, bool):
        return exact
    for key, value in results.items():
        if key.startswith(f"{evaluator}.") and not key.endswith(("_threshold", "_result")):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return None


def grade(args, records, out_dir, credential):
    """azure-ai-evaluation 으로 채점하고 결과를 올린다.

    evaluate() 는 메모리가 아니라 경로에서 읽어서 행을 먼저 디스크에 쓴다. 통째로 감싼 이유는,
    역할 할당이 없으면 judge 호출을 전부 지불한 뒤에 실패하기 때문이다. 업로드와 함께 리포트까지
    잃으면 같은 것을 배우려고 두 번 내는 셈이 된다.
    """
    module = import_evaluation()
    if module is None or credential is None:
        return {}

    path = os.path.join(out_dir, "rows.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    config = {"azure_endpoint": account_endpoint(args.endpoint),
              "azure_deployment": args.judge_deployment,
              "api_version": JUDGE_API_VERSION}
    # 추론 모델 judge 는 temperature 와 top_p 를 거부하는데, 라이브러리는 말해줘야만 뺀다.
    # 이게 없으면 gpt-5 계열 judge 가 모든 행에서 실패한다.
    options = {"credential": credential, "threshold": EVALUATOR_THRESHOLD,
               "is_reasoning_model": args.judge_reasoning}
    evaluators = {name: cls(config, **options) for name, cls in module["classes"].items()}
    mapping = {"groundedness": ["query", "response", "context"],
               "relevance": ["query", "response"],
               "intent_resolution": ["query", "response"],
               "tool_call_accuracy": ["query", "tool_calls", "tool_definitions"]}
    evaluator_config = {
        name: {"column_mapping": {column: f"${{data.{column}}}" for column in columns}}
        for name, columns in mapping.items()}

    try:
        result = module["evaluate"](
            data=path, evaluators=evaluators, evaluator_config=evaluator_config,
            evaluation_name="harness-step3",
            azure_ai_project=args.endpoint if args.upload else None,
            credential=credential)
    except Exception as error:  # noqa: BLE001 — 채점은 이미 값을 치렀으니 행이라도 남긴다
        print(f"  [채점 실패, 행은 {path} 에 남겼습니다] {error}")
        return {}

    scores = {name: pick_metric(result.get("metrics", {}), name) for name in mapping}
    if result.get("studio_url"):
        scores["studio_url"] = result["studio_url"]
    return scores


def parse_args():
    parser = harness_cli.build_parser(
        description="step 3 — 평가 레이어를 짓는다: 실행을 기록하고, 회귀를 잡는다.",
        epilog="기본값인 --judge none 으로도 hit · 헛수고 · token · tool error 가 나옵니다. "
               "--judge evaluation 은 채점 점수를 더하고 행마다 비용이 듭니다.",
    )
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="검증 레이어를 빼고 잰다 — 검증이 지표를 얼마나 움직였는지 볼 때")
    parser.add_argument("--out", default=None, metavar="JSON",
                        help="나중 실행이 대조할 수 있도록 이번 실행을 기록한다")
    parser.add_argument("--baseline", default=None, metavar="JSON",
                        help="앞선 --out 파일. 그 뒤로 무엇이 나빠졌는지 보고한다")
    parser.add_argument("--judge", choices=["none", "evaluation"], default="none")
    parser.add_argument("--judge-deployment", default="gpt-4.1", help="채점하는 모델")
    parser.add_argument("--judge-reasoning", action="store_true",
                        help="채점 모델이 추론 모델일 때 켠다")
    parser.add_argument("--no-upload", dest="upload", action="store_false",
                        help="Foundry Evaluation 탭에 올리지 않고 로컬에서만 채점")
    args = harness_cli.finish_parsing(parser)
    if args.judge == "evaluation" and args.upload and "/api/projects/" not in args.endpoint:
        parser.error("업로드하려면 --endpoint 가 project 엔드포인트(.../api/projects/<이름>)여야 "
                     "합니다. 아니면 --no-upload 를 주세요")
    return args


def main():
    args = parse_args()
    ctx = {**harness_cli.prepare(args), "run": metrics.new_run()}

    metrics.header(3, "평가", "바꾼 것이 나빠졌는지 알려주는 장치")
    metrics.overview(DOES, WATCH, [
        ("모델", args.deployment),
        ("하네스", "tool + 검증 + 재시도" if args.verify else "tool 만 (검증 뺌)"),
        ("질문", f"{len(ctx['golden'])}개"),
        ("채점", "azure-ai-evaluation (행마다 비용 발생)"
                 if args.judge == "evaluation" else "없음 — hit 은 문자열 대조라 공짜입니다"),
        ("대조", args.baseline or "없음 (--baseline 으로 앞선 기록을 주세요)"),
    ])

    started = time.perf_counter()
    records = answer_all(ctx, args.verify)
    elapsed = time.perf_counter() - started
    hits = sum(1 for record in records if record["hit"])
    wasted = sum(record["wasted"] for record in records)
    saved = sum(1 for record in records if not record["first_hit"] and record["hit"])

    extra = {}
    if args.judge == "evaluation":
        # api-key 와 access-token 은 credential 객체를 만들지 않는데 judge 는 그게 필요하다.
        credential = None
        if args.auth not in ("api-key", "access-token"):
            import identity  # noqa: PLC0415 — 이 분기에서만 필요하다
            credential = identity.get_credential(args)
        else:
            print("  [--auth api-key/access-token 으로는 judge 에 서명할 수 없어 채점을 건너뜁니다]")
        out_dir = tempfile.mkdtemp(prefix="harness-eval-")
        for name, value in grade(args, records, out_dir, credential).items():
            extra[name] = f"{value:.2f}" if isinstance(value, float) else value

    extra["검증이 살린 답"] = f"{saved}개"
    extra["헛수고"] = f"{wasted}번 (맞는 답을 반려)"

    headline = f"질문 {len(records)}개 중 {hits}개를 맞혔습니다."
    if args.baseline:
        regressions, fixes, drift = compare(records, args.baseline)
        if regressions:
            headline += f" 전에는 되던 {len(regressions)}개가 이번에 안 됩니다."
        elif fixes:
            headline += f" 안 되던 {len(fixes)}개가 이번에 됩니다. 나빠진 것은 없습니다."
        else:
            headline += " 앞선 기록과 견줘 통과 여부가 바뀐 질문은 없습니다."
    else:
        headline += " 이 결과를 --out 으로 남겨두면 다음 실행이 여기에 견줄 수 있습니다."

    metrics.summary("평가", headline, ctx["run"], elapsed, hits, len(records), extra=extra)

    if args.baseline:
        print(f" {args.baseline} 과 비교")
        print(f"   회귀       {', '.join(regressions) if regressions else '없음'}")
        print(f"   개선       {', '.join(fixes) if fixes else '없음'}")
        print(f"   호출 증가  {', '.join(drift) if drift else '없음'}")
        if regressions:
            print("\n 회귀는 전에는 통과하던 질문입니다. 고치든, 알고서 받아들이든 하세요 —")
            print(" 다만 그 사실을 사용자에게서 전해 듣지는 마세요.")
        print()

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"deployment": args.deployment, "records": records}, handle,
                      ensure_ascii=False, indent=2)
        print(f" 기록했습니다: {args.out}")
        print(" 이제 harness_verify.py 의 판정 지시문이나 step1_tools.py 의 description 을 바꾼 뒤")
        print(f" --baseline {args.out} 으로 다시 돌려 보세요.\n")


if __name__ == "__main__":
    main()
