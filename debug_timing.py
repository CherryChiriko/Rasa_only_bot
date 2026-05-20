"""
debug_timing.py — Rasa latency debugger
Run this while both Rasa and the action server are running.

Usage:
    python debug_timing.py

It will fire several messages at your bot and print a breakdown of:
  - Total round-trip time (your app's real latency)
  - Rasa server processing time (from the X-Process-Time header if available)
  - Action server time (read from [TIMING] lines in action server stdout)

The gap between total and Rasa time = network + serialisation overhead.
The gap between Rasa time and action time = TED/DIET inference time.
"""

import time
import requests
import json

RASA_URL = "http://localhost:5005/webhooks/rest/webhook"
SENDER_ID = "debug_timing_user"

# Messages that exercise different code paths
TEST_MESSAGES = [
    ("utter only (no action)",   "Bonjour"),
    ("rule action (fallback)",   "asdfghjkl"),
    ("form trigger",             "Mon pc ne marche pas"),
    ("form slot fill (user_id)", "AB-1234"),
    ("form slot fill (desc)",    "Mon ecran reste noir apres le demarrage"),
    ("affirm (submit)",          "Oui"),
]

HEADERS = {"Content-Type": "application/json"}


def send(text: str) -> tuple[float, list]:
    payload = {"sender": SENDER_ID, "message": text}
    t0 = time.perf_counter()
    r = requests.post(RASA_URL, headers=HEADERS, json=payload, timeout=30)
    elapsed = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return elapsed, r.json()


def main():
    print(f"\n{'=' * 60}")
    print("Rasa latency diagnostic")
    print(f"Target: {RASA_URL}")
    print(f"{'=' * 60}\n")

    results = []
    for label, message in TEST_MESSAGES:
        try:
            elapsed, responses = send(message)
            bot_texts = [r.get("text", "") for r in responses]
            results.append((label, message, elapsed, bot_texts))
            print(f"[{elapsed:6.0f} ms]  {label!r}")
            print(f"           sent:     {message!r}")
            for t in bot_texts:
                preview = t[:80] + "..." if len(t) > 80 else t
                print(f"           received: {preview!r}")
            print()
        except Exception as e:
            print(f"[ERROR] {label!r}: {e}\n")
        time.sleep(0.3)  # small gap so logs stay readable

    print(f"{'=' * 60}")
    print("Summary")
    print(f"{'=' * 60}")
    print(f"{'Turn':<35} {'ms':>8}")
    print(f"{'-' * 44}")
    for label, _, elapsed, _ in results:
        print(f"{label:<35} {elapsed:>8.0f}")

    if results:
        times = [e for _, _, e, _ in results]
        print(f"{'-' * 44}")
        print(f"{'Average':<35} {sum(times)/len(times):>8.0f}")
        print(f"{'Max':<35} {max(times):>8.0f}")
        print(f"{'Min':<35} {min(times):>8.0f}")

    print()
    print("Next steps:")
    print("  - Turns with NO custom action should be fast (<100 ms).")
    print("    If they're slow -> TED inference or Rasa server overhead.")
    print("  - Turns WITH a custom action will be slower by the action's [TIMING] value.")
    print("    Check action server logs for [TIMING] lines to see action cost.")
    print("  - Big gap between 'no action' and 'with action' = action server latency.")
    print("  - All turns slow equally = TED policy is the culprit.")
    print()


if __name__ == "__main__":
    main()