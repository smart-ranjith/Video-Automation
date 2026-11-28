#!/usr/bin/env python3
"""weekly_audit.py – Full automated channel audit.

Combines all tools from tools/ (ported from jakeschincariol/youtube-agent-skill)
and the analytics_engine weekly dashboard. Designed to be run as a scheduled
task (Windows Task Scheduler / GitHub Actions cron) every Monday.

Usage:
    python weekly_audit.py              # run only if due
    python weekly_audit.py --force      # always run
    python weekly_audit.py --local-only # run tools on local files only (no API)
"""
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

REPORT_PATH = os.path.join(SCRIPT_DIR, "weekly_audit_report.json")


def run_local_tools() -> dict:
    """Run all zero-API tools on local files if present."""
    result = {}

    # 1. Hook score of the most recent script (first line of voiceover.mp3 substitute)
    srt_path = os.path.join(SCRIPT_DIR, "subtitles.srt")
    if os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
        try:
            from tools.deadair import build_edl, load_cues
            cues = load_cues(srt_path)
            if cues:
                full_script = " ".join(c[2] for c in cues)
                # Score hook (first spoken sentence)
                from tools.hookscore import score_hook
                first_sentence = full_script.split(".")[0]
                hook_result = score_hook(first_sentence)
                result["last_hook_score"] = {
                    "sentence": first_sentence[:120],
                    "verdict": hook_result["verdict"],
                    "band": hook_result["band"],
                    "weakest": hook_result["weakest"],
                    "fix_tip": hook_result["fix_tip"],
                }
                print(f"🎣 Hook Score: {hook_result['verdict']} [{hook_result['band']}] "
                      f"– weakest: {hook_result['weakest']}")
                print(f"   Fix: {hook_result['fix_tip']}")
        except Exception as e:
            print(f"⚠️ Hook score skipped: {e}")

        # 2. Dead-air EDL
        try:
            from tools.deadair import build_edl
            edl = build_edl(srt_path)
            if "error" not in edl:
                result["edit_decision_list"] = {
                    "summary": edl["summary"],
                    "cuts": len(edl["cuts"]),
                    "removable_sec": edl["removed_sec"],
                }
                print(f"✂️  EDL: {edl['summary']}")
        except Exception as e:
            print(f"⚠️ EDL skipped: {e}")

        # 3. Chapter timestamps
        try:
            from tools.chapters import generate_chapters
            ch = generate_chapters(srt_path, target=7)
            if ch.get("valid"):
                result["chapters"] = {
                    "count": len(ch["chapters"]),
                    "valid": True,
                    "paste_block": ch["paste_block"],
                }
                print(f"📌 Chapters ({len(ch['chapters'])}): valid={ch['valid']}")
            elif "error" not in ch:
                print("📌 Chapters: not enough cues for a valid chapter list")
        except Exception as e:
            print(f"⚠️ Chapters skipped: {e}")

    # 4. Retention CSV audit
    csv_path = os.path.join(SCRIPT_DIR, "retention.csv")
    if os.path.exists(csv_path):
        try:
            from tools.retention_audit import analyze_csv
            ret = analyze_csv(
                csv_path,
                transcript_path=srt_path if os.path.exists(srt_path) else None,
            )
            if "error" not in ret:
                result["retention_audit"] = ret
                print(f"\n📉 Retention CSV Audit:\n{ret['summary']}")
        except Exception as e:
            print(f"⚠️ Retention audit skipped: {e}")

    # 5. Title lint (last packaged title)
    packaging_path = os.path.join(SCRIPT_DIR, "packaging_ab_test.json")
    if os.path.exists(packaging_path):
        try:
            pkg = json.load(open(packaging_path))
            title = pkg.get("title_a") or pkg.get("title") or ""
            thumb = pkg.get("thumb_a") or pkg.get("thumbnail_text") or ""
            if title:
                from tools.titlelint import lint_title
                lint = lint_title(title, thumb or None)
                result["title_lint"] = lint
                status = "✓ PASS" if lint["pass"] else "✗ FAIL"
                print(f"📋 Title Lint [{lint['score']}/100] {status}: {title[:60]}")
                for k, m in lint["issues"]:
                    print(f"   ✗ {k}: {m}")
        except Exception as e:
            print(f"⚠️ Title lint skipped: {e}")

    # 6. Niche outlier formula analysis (if collected_outliers.json exists)
    outliers_path = os.path.join(SCRIPT_DIR, "collected_outliers.json")
    if os.path.exists(outliers_path):
        try:
            from tools.swipe import find_outliers
            with open(outliers_path, encoding="utf-8") as fh:
                rows = json.load(fh)
            sw = find_outliers(rows, min_multiple=1.5)
            result["niche_outliers"] = {
                "outlier_count": len(sw["outliers"]),
                "top_formulas": sw["formula_counts"],
                "top_3": sw["outliers"][:3],
            }
            print(f"🚀 Niche Outliers: {len(sw['outliers'])} found | Top formulas: {sw['formula_counts']}")
        except Exception as e:
            print(f"⚠️ Niche outlier scan skipped: {e}")

    return result


def distill_one_fix(report: dict) -> dict:
    """yt-audit rule: end in ONE prioritized fix rather than twenty items."""
    local = report.get("local_tools", {})
    dash = report.get("dashboard", {})
    ret_csv = local.get("retention_audit", {})
    ret_api = dash.get("retention_insights", {})
    hook = local.get("last_hook_score", {})
    title_lint = local.get("title_lint", {})
    div = dash.get("topic_diversity", {})

    working_well = []
    if hook.get("verdict", 0) >= 72:
        working_well.append(f"Opening hook scored {hook['verdict']}/100 ({hook.get('band')}).")
    if title_lint.get("pass"):
        working_well.append(f"Title + thumbnail packaging passed lint ({title_lint.get('score')}/100).")
    if div.get("diversity_score", 100) >= 70:
        working_well.append(f"Topic diversity is healthy ({div.get('diversity_score')}/100).")

    # Priority order from yt-audit: Packaging/Title -> First 15s Hook -> Retention Leak -> Topic Diversity
    one_fix = "Keep current cadence and monitor next 7-day retention curve."
    do_not_do = "Do not rewrite the middle of scripts before fixing the opening 5 seconds."

    if title_lint and not title_lint.get("pass", True):
        issues = ", ".join(k for k, _ in title_lint.get("issues", []))
        one_fix = f"Fix title/thumbnail packaging ({issues}) before next upload — aim for >= 60/100 lint score."
    elif hook and hook.get("verdict", 100) < 55:
        one_fix = f"Rewrite opening hook (scored {hook['verdict']}/100, weakest: {hook.get('weakest')}). {hook.get('fix_tip', '')}"
    elif ret_csv and ret_csv.get("hook_leak", 0) >= 25:
        one_fix = f"Fix 30s hook leak ({ret_csv['hook_leak']}% lost [{ret_csv.get('hook_verdict')}]): tighten the first sentence to confirm the title immediately."
    elif ret_api.get("weak_3s_hook_hold_pct") is not None and ret_api.get("weak_3s_hook_hold_pct", 100) < 65:
        one_fix = f"Raise weak-video 3s hook hold ({ret_api['weak_3s_hook_hold_pct']}% vs winners {ret_api.get('winner_3s_hook_hold_pct')}%): cut faster at 0-3s."
    elif div and div.get("diversity_score", 100) < 55:
        banned = ", ".join(div.get("saturated_themes_to_ban", [])[:4])
        one_fix = f"Break topic repetition (diversity {div['diversity_score']}/100): avoid {banned} this week."

    return {
        "one_fix": one_fix,
        "working_well": working_well,
        "do_not_do_yet": do_not_do,
    }


def run_full_audit(force: bool = False, local_only: bool = False) -> dict:
    """Run the full weekly audit. Returns the combined report dict."""
    print("=" * 60)
    print("🔎 VIDEO AUTOMATION – WEEKLY AUDIT")
    print("=" * 60)

    report = {"local_tools": {}, "dashboard": {}}

    # Always run local zero-API tools
    report["local_tools"] = run_local_tools()

    # Run YouTube Analytics dashboard unless --local-only
    if not local_only:
        try:
            from analytics_engine import run_weekly_dashboard_analysis
            strategy = run_weekly_dashboard_analysis(force=force)
            if strategy:
                report["dashboard"] = strategy
        except Exception as e:
            print(f"⚠️ Dashboard analysis skipped: {e}")
            report["dashboard"] = {"error": str(e)}

    # Distill ONE prioritized fix (yt-audit)
    report["audit_verdict"] = distill_one_fix(report)
    print(f"\n🎯 ONE FIX THIS WEEK: {report['audit_verdict']['one_fix']}")

    # Save combined report
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\n✅ Weekly audit complete. Report saved → {REPORT_PATH}")
    return report


if __name__ == "__main__":
    force = "--force" in sys.argv
    local_only = "--local-only" in sys.argv
    run_full_audit(force=force, local_only=local_only)
