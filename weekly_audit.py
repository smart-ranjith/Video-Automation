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

    return result


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

    # Save combined report
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\n✅ Weekly audit complete. Report saved → {REPORT_PATH}")
    return report


if __name__ == "__main__":
    force = "--force" in sys.argv
    local_only = "--local-only" in sys.argv
    run_full_audit(force=force, local_only=local_only)
