#!/usr/bin/env python3
"""
Auto CPU Scheduling Analyzer
============================
Operating Systems course project.

NO user input is needed. The program:
  1. Probes the machine (CPU clock speed, core count, RAM, disk/network throughput,
     compute speed, measured context-switch latency).
  2. Samples the REAL processes running right now and turns them into a workload:
        arrival time  <- process creation time
        burst time    <- CPU time the process has actually consumed (scaled to ms)
        priority      <- OS nice value (+ interactive boost for I/O-heavy processes)
  3. Simulates FCFS, SJF, SRTF, Priority (non-preemptive & preemptive),
     Round Robin (auto-tuned quantum) and MLFQ on that workload.
  4. Scores every algorithm and tells you which one is best for THIS workload,
     printing a terminal report and writing an HTML report with Gantt charts.

Run:   pip install psutil
       python os_scheduler_analyzer.py
Options: --n 15 (number of processes)   --no-browser   --out report.html
"""
import argparse
import html
import math
import os
import platform
import statistics
import sys
import threading
import time
import webbrowser

try:
    import psutil
except ImportError:
    sys.exit("psutil is required:  pip install psutil")


# ----------------------------------------------------------------------------
# 1. SYSTEM PROBING
# ----------------------------------------------------------------------------
def measure_context_switch(rounds=3000):
    """Ping-pong between two threads over pipes; returns microseconds per switch."""
    a_r, a_w = os.pipe()
    b_r, b_w = os.pipe()

    def echo():
        for _ in range(rounds):
            os.read(a_r, 1)
            os.write(b_w, b"x")

    th = threading.Thread(target=echo)
    th.start()
    t0 = time.perf_counter()
    for _ in range(rounds):
        os.write(a_w, b"x")
        os.read(b_r, 1)
    dt = time.perf_counter() - t0
    th.join()
    for fd in (a_r, a_w, b_r, b_w):
        os.close(fd)
    return dt / (rounds * 2) * 1e6


def measure_compute_speed():
    """Tiny CPU benchmark -> million simple operations per second."""
    n = 2_000_000
    t0 = time.perf_counter()
    x = 0
    for i in range(n):
        x += i * 3 % 7
    dt = time.perf_counter() - t0
    return n / dt / 1e6


def probe_system(window=1.0):
    """Collect hardware/system facts. Also measures disk & network throughput
    over `window` seconds (used as the I/O-pressure indicator)."""
    d0, n0 = psutil.disk_io_counters(), psutil.net_io_counters()
    psutil.cpu_percent(None)
    t0 = time.perf_counter()
    time.sleep(window)
    el = time.perf_counter() - t0
    d1, n1 = psutil.disk_io_counters(), psutil.net_io_counters()
    freq = psutil.cpu_freq()
    info = {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": platform.processor() or platform.machine(),
        "cores_phys": psutil.cpu_count(logical=False) or psutil.cpu_count(),
        "cores_log": psutil.cpu_count(),
        "freq_mhz": freq.current if freq and freq.current else 2000.0,
        "ram_gb": psutil.virtual_memory().total / 2**30,
        "cpu_load": psutil.cpu_percent(None),
        "disk_mbps": ((d1.read_bytes + d1.write_bytes) - (d0.read_bytes + d0.write_bytes)) / el / 2**20 if d0 and d1 else 0.0,
        "net_down_kbps": (n1.bytes_recv - n0.bytes_recv) / el / 1024,
        "net_up_kbps": (n1.bytes_sent - n0.bytes_sent) / el / 1024,
        "cs_us": measure_context_switch(),
        "mops": measure_compute_speed(),
    }
    return info


# ----------------------------------------------------------------------------
# 2. WORKLOAD BUILT FROM REAL PROCESSES
# ----------------------------------------------------------------------------
WIN_NICE = {}
if os.name == "nt":
    for _n, _v in (("IDLE_PRIORITY_CLASS", 19), ("BELOW_NORMAL_PRIORITY_CLASS", 10),
                   ("NORMAL_PRIORITY_CLASS", 0), ("ABOVE_NORMAL_PRIORITY_CLASS", -5),
                   ("HIGH_PRIORITY_CLASS", -10), ("REALTIME_PRIORITY_CLASS", -20)):
        if hasattr(psutil, _n):
            WIN_NICE[getattr(psutil, _n)] = _v


def build_workload(freq_mhz, n=12, window=1.0):
    me = os.getpid()
    snap = {}
    for p in psutil.process_iter():
        try:
            t = p.cpu_times()
            snap[p.pid] = (p, t.user + t.system)
        except Exception:
            pass
    time.sleep(window)  # second sample -> recent CPU activity
    rows = []
    for pid, (p, c0) in snap.items():
        if pid in (0, me):
            continue
        try:
            t = p.cpu_times()
            c1 = t.user + t.system
            name = p.name()
            ct = p.create_time()
        except Exception:
            continue
        try:
            io = p.io_counters()
            iob = io.read_bytes + io.write_bytes
        except Exception:
            iob = 0
        try:
            nice = p.nice()
            if os.name == "nt":
                nice = WIN_NICE.get(nice, 0)
        except Exception:
            nice = 0
        if c1 <= 0.0:
            continue
        rows.append(dict(pid=pid, name=name[:18], create=ct, cpu_s=c1,
                         recent=max(0.0, c1 - c0), io_bytes=iob, nice=nice))
    if len(rows) < 4:
        sys.exit("Not enough active processes found to build a workload.")

    # most CPU-active processes first (recent activity, then lifetime CPU time)
    rows.sort(key=lambda r: (r["recent"], r["cpu_s"]), reverse=True)
    rows = rows[:n]

    # burst: lifetime CPU seconds, log-compressed into 5..100 ms so the
    # simulation stays readable while preserving the real ordering
    mx = max(r["cpu_s"] for r in rows)
    for r in rows:
        r["burst"] = int(round(5 + 95 * math.log1p(r["cpu_s"]) / math.log1p(mx)))
        r["mcycles"] = r["cpu_s"] * freq_mhz           # CPU work in millions of cycles
        r["io_mb_per_cpu_s"] = r["io_bytes"] / 2**20 / max(r["cpu_s"], 1e-3)
        r["kind"] = "I/O-bound" if r["io_mb_per_cpu_s"] > 5 else "CPU-bound"
        # priority: lower number = more important (Linux style: nice + 20)
        pr = r["nice"] + 20
        if r["kind"] == "I/O-bound":
            pr -= 3                                      # interactive boost
        r["priority"] = max(0, min(39, pr))

    # arrival: real creation order, stretched over 60% of total work so processes overlap
    oldest = min(r["create"] for r in rows)
    span = max(r["create"] for r in rows) - oldest
    total = sum(r["burst"] for r in rows)
    for r in rows:
        r["arrival"] = int(round((r["create"] - oldest) / span * 0.6 * total)) if span > 0 else 0
    rows.sort(key=lambda r: (r["arrival"], r["pid"]))
    return rows


# ----------------------------------------------------------------------------
# 3. SCHEDULING POLICIES + SIMULATION ENGINE (1 ms ticks)
# ----------------------------------------------------------------------------
class Policy:
    name = ""
    def reset(self): pass
    def pick(self, ready, cur, t): raise NotImplementedError
    def tick(self, p): pass


class Keyed(Policy):
    """FCFS / SJF / SRTF / Priority: pick the min of a key, optionally preemptive."""
    def __init__(self, name, key, preemptive):
        self.name, self.key, self.preemptive = name, key, preemptive

    def pick(self, ready, cur, t):
        alive = cur is not None and cur["rem"] > 0
        if alive and not self.preemptive:
            return cur
        return min(ready, key=lambda p: (self.key(p), p is not cur, p["arrival"], p["pid"]))


class RR(Policy):
    def __init__(self, q):
        self.q = q
        self.name = f"Round Robin (q={q}ms)"

    def reset(self):
        self.queue, self.inq, self.used = [], set(), 0

    def _enq(self, p):
        self.queue.append(p)
        self.inq.add(p["pid"])

    def pick(self, ready, cur, t):
        alive = cur is not None and cur["rem"] > 0
        for p in sorted(ready, key=lambda p: (p["arrival"], p["pid"])):
            if p["pid"] not in self.inq and p is not cur:
                self._enq(p)
        if alive and self.used < self.q:
            return cur
        if alive:
            self._enq(cur)
        nxt = self.queue.pop(0)
        self.inq.discard(nxt["pid"])
        self.used = 0
        return nxt

    def tick(self, p):
        self.used += 1


class MLFQ(Policy):
    """3 queues; new jobs start on top, use up their slice -> demoted; higher queue preempts."""
    def __init__(self, q):
        self.qs = [max(2, q // 2), q, q * 2]
        self.name = f"MLFQ (slices {self.qs[0]}/{self.qs[1]}/{self.qs[2]}ms)"

    def reset(self):
        self.qu, self.lvl, self.used = [[], [], []], {}, 0

    def pick(self, ready, cur, t):
        alive = cur is not None and cur["rem"] > 0
        for p in sorted(ready, key=lambda p: (p["arrival"], p["pid"])):
            if p["pid"] not in self.lvl:
                self.lvl[p["pid"]] = 0
                self.qu[0].append(p)
        if alive:
            L = self.lvl[cur["pid"]]
            if any(self.qu[i] for i in range(L)):
                self.qu[L].append(cur)
            elif self.used >= self.qs[L]:
                L2 = min(L + 1, 2)
                self.lvl[cur["pid"]] = L2
                self.qu[L2].append(cur)
            else:
                return cur
        for L in range(3):
            if self.qu[L]:
                self.used = 0
                return self.qu[L].pop(0)

    def tick(self, p):
        self.used += 1


def simulate(procs, policy, cs_ms):
    P = [dict(p, rem=p["burst"], start=None, finish=None) for p in procs]
    policy.reset()
    t, cur, last = 0.0, None, None
    switches, overhead, tl, done = 0, 0.0, [], 0
    while done < len(P):
        ready = [p for p in P if p["arrival"] <= t and p["rem"] > 0]
        if not ready:
            t = min(p["arrival"] for p in P if p["rem"] > 0)
            cur = last = None
            continue
        ch = policy.pick(ready, cur, t)
        if last is not None and ch is not last:         # dispatch / context switch
            t += cs_ms
            overhead += cs_ms
            switches += 1
        if ch["start"] is None:
            ch["start"] = t
        ch["rem"] -= 1
        if tl and tl[-1][0] == ch["pid"] and abs(tl[-1][2] - t) < 1e-9:
            tl[-1][2] = t + 1
        else:
            tl.append([ch["pid"], t, t + 1])
        t += 1
        policy.tick(ch)
        cur = last = ch
        if ch["rem"] == 0:
            ch["finish"] = t
            done += 1

    n = len(P)
    tat = [p["finish"] - p["arrival"] for p in P]
    wait = [tat[i] - P[i]["burst"] for i in range(n)]
    resp = [p["start"] - p["arrival"] for p in P]
    slow = [tat[i] / P[i]["burst"] for i in range(n)]
    jain = sum(slow) ** 2 / (n * sum(s * s for s in slow))
    busy = sum(p["burst"] for p in P)
    return dict(name=policy.name, wait=sum(wait) / n, tat=sum(tat) / n, resp=sum(resp) / n,
                maxwait=max(wait), fair=jain, switches=switches, overhead=overhead,
                util=busy / t * 100, thr=n / t * 1000, makespan=t, timeline=tl)


# ----------------------------------------------------------------------------
# 4. SCORING + RECOMMENDATION
# ----------------------------------------------------------------------------
NOTES = {
    "FCFS": "simple and starvation-free, but short jobs get stuck behind long ones (convoy effect).",
    "SJF": "minimises average waiting among non-preemptive schemes, but needs burst prediction and can starve long jobs.",
    "SRTF": "theoretically optimal for average waiting; needs burst prediction, many switches and can starve long jobs.",
    "Priority (non-preemptive)": "honours OS priorities, but low-priority jobs can starve without aging.",
    "Priority (preemptive)": "urgent work runs immediately, but low-priority jobs can starve without aging.",
    "Round Robin": "very fair with good response time; cost is more context switches and longer turnaround.",
    "MLFQ": "approximates SJF without knowing burst times and keeps interactive jobs snappy; most complex to tune.",
}


def note_for(name):
    for k, v in NOTES.items():
        if name.startswith(k):
            return v
    return ""


def score(results, profile):
    w = {"wait": .30, "tat": .15, "resp": .20, "maxwait": .10, "switches": .05, "fair": .20}
    if profile["io_share"] > 0.4:        # interactive-heavy -> care about response time
        w["resp"] += .10; w["tat"] -= .05; w["wait"] -= .05
    if profile["cv"] > 1.0:              # very uneven bursts -> convoy / starvation risk
        w["wait"] += .05; w["maxwait"] += .05; w["fair"] -= .05; w["tat"] -= .05

    def bad(key, higher_better=False):
        vals = [r[key] for r in results]
        lo, hi = min(vals), max(vals)
        if hi == lo:
            return [0.0] * len(results)
        return [((hi - v) if higher_better else (v - lo)) / (hi - lo) for v in vals]

    parts = {k: bad(k, k == "fair") for k in w}
    for i, r in enumerate(results):
        r["score"] = round(100 * (1 - sum(w[k] * parts[k][i] for k in w)), 1)
    results.sort(key=lambda r: r["score"], reverse=True)
    return w


# ----------------------------------------------------------------------------
# 5. REPORTS
# ----------------------------------------------------------------------------
def terminal_report(sysinfo, procs, results, profile, weights, quantum):
    line = "=" * 100
    print(line)
    print(" AUTO CPU SCHEDULING ANALYZER  (no manual input - everything measured from this machine)")
    print(line)
    print(f" OS: {sysinfo['os']} | CPU: {sysinfo['cpu']} | {sysinfo['cores_phys']} cores / {sysinfo['cores_log']} threads"
          f" @ {sysinfo['freq_mhz']:.0f} MHz | RAM {sysinfo['ram_gb']:.1f} GB")
    print(f" Load now: {sysinfo['cpu_load']:.0f}% | Disk {sysinfo['disk_mbps']:.2f} MB/s | "
          f"Net down {sysinfo['net_down_kbps']:.1f} KB/s, up {sysinfo['net_up_kbps']:.1f} KB/s")
    print(f" Measured context-switch latency: {sysinfo['cs_us']:.1f} us | Compute speed: {sysinfo['mops']:.1f} Mops/s")
    print(f"\n WORKLOAD: {len(procs)} live processes | burst CV = {profile['cv']:.2f} | "
          f"I/O-bound share = {profile['io_share']*100:.0f}% | RR quantum (80th pct of bursts) = {quantum} ms\n")
    print(f" {'PID':>7} {'Process':<18} {'Arr':>5} {'Burst':>6} {'Prio':>5} {'Mcycles':>9}  Type")
    for p in procs:
        print(f" {p['pid']:>7} {p['name']:<18} {p['arrival']:>5} {p['burst']:>6} {p['priority']:>5} {p['mcycles']:>9.0f}  {p['kind']}")
    print("\n" + line)
    print(f" {'#':<2} {'Algorithm':<30} {'AvgWait':>8} {'AvgTAT':>8} {'AvgResp':>8} {'MaxWait':>8} {'CtxSw':>6} {'CPU%':>6} {'Fair':>5} {'SCORE':>6}")
    print("-" * 100)
    for i, r in enumerate(results, 1):
        print(f" {i:<2} {r['name']:<30} {r['wait']:>8.1f} {r['tat']:>8.1f} {r['resp']:>8.1f} {r['maxwait']:>8.1f} "
              f"{r['switches']:>6} {r['util']:>6.1f} {r['fair']:>5.2f} {r['score']:>6.1f}")
    print(line)
    best = results[0]
    print(f"\n >>> RECOMMENDED FOR THE CURRENT PROCESSES: {best['name']}  (score {best['score']}/100)")
    why = []
    for key, label, low in (("wait", "lowest average waiting time", True), ("resp", "best response time", True),
                            ("tat", "lowest turnaround", True), ("fair", "fairest", False)):
        champ = min(results, key=lambda r: r[key]) if low else max(results, key=lambda r: r[key])
        if champ is best:
            why.append(label)
    print(f"     Why: {', '.join(why) if why else 'best overall balance across all metrics'}.")
    print(f"     Note: {note_for(best['name'])}")
    print(f"     Runner-up: {results[1]['name']} ({results[1]['score']}).")
    print(f"     Workload reasoning: bursts are {'very uneven' if profile['cv'] > 1 else 'fairly even'}"
          f"{' and mostly interactive/I-O heavy' if profile['io_share'] > 0.4 else ''}, so the scoring weights were "
          f"{'adjusted' if (profile['cv'] > 1 or profile['io_share'] > 0.4) else 'left at defaults'}.\n")


def color(pid):
    return f"hsl({(pid * 47) % 360},65%,55%)"


def html_report(path, sysinfo, procs, results, profile, quantum):
    esc = html.escape
    rows = "".join(
        f"<tr><td>{p['pid']}</td><td>{esc(p['name'])}</td><td>{p['arrival']}</td><td>{p['burst']}</td>"
        f"<td>{p['priority']}</td><td>{p['mcycles']:.0f}</td><td>{p['kind']}</td></tr>" for p in procs)
    res = "".join(
        f"<tr{' class=best' if i == 0 else ''}><td>{i+1}</td><td>{esc(r['name'])}</td><td>{r['wait']:.1f}</td>"
        f"<td>{r['tat']:.1f}</td><td>{r['resp']:.1f}</td><td>{r['maxwait']:.1f}</td><td>{r['switches']}</td>"
        f"<td>{r['util']:.1f}</td><td>{r['fair']:.2f}</td><td><b>{r['score']}</b></td></tr>"
        for i, r in enumerate(results, 1))
    bars = "".join(
        f"<div class=bar><span>{esc(r['name'])}</span><div class=track><div class=fill style='width:{r['score']}%'>{r['score']}</div></div></div>"
        for r in results)
    mk = max(r["makespan"] for r in results)
    gantt = ""
    for r in results:
        segs = "".join(
            f"<div class=seg style='left:{s/mk*100:.3f}%;width:{max((e-s)/mk*100,.15):.3f}%;background:{color(pid)}' "
            f"title='PID {pid}: {s:.0f}-{e:.0f} ms'></div>" for pid, s, e in r["timeline"])
        gantt += f"<div class=g><div class=gl>{esc(r['name'])}</div><div class=gt>{segs}</div></div>"
    legend = "".join(f"<span class=chip style='background:{color(p['pid'])}'>{esc(p['name'])} ({p['pid']})</span>" for p in procs)
    best = results[0]
    doc = f"""<!doctype html><html><head><meta charset=utf-8><title>Scheduling Analyzer Report</title>
<style>
body{{font-family:system-ui,Segoe UI,Arial;margin:30px auto;max-width:1100px;padding:0 16px;color:#1d2433;background:#f6f7fb}}
h1{{margin-bottom:4px}} h2{{margin-top:34px}} .card{{background:#fff;border-radius:12px;padding:18px 22px;box-shadow:0 1px 4px #0001}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{padding:6px 10px;border-bottom:1px solid #e6e8ef;text-align:right}}
th:nth-child(2),td:nth-child(2){{text-align:left}} tr.best{{background:#e6f7ea}}
.winner{{background:#16a34a;color:#fff;border-radius:12px;padding:18px 22px;font-size:20px}}
.bar{{display:flex;align-items:center;margin:6px 0}} .bar span{{width:260px;font-size:14px}}
.track{{flex:1;background:#e6e8ef;border-radius:6px}} .fill{{background:#2563eb;color:#fff;border-radius:6px;padding:3px 8px;font-size:13px;box-sizing:border-box}}
.g{{display:flex;align-items:center;margin:8px 0}} .gl{{width:260px;font-size:13px}} .gt{{position:relative;flex:1;height:26px;background:#eceef5;border-radius:4px}}
.seg{{position:absolute;top:0;height:100%}} .chip{{display:inline-block;color:#fff;border-radius:10px;padding:2px 9px;margin:3px;font-size:12px}}
.small{{color:#5b6475;font-size:13px}}
</style></head><body>
<h1>Auto CPU Scheduling Analyzer</h1><div class=small>Generated {time.strftime('%Y-%m-%d %H:%M:%S')} - all data measured automatically, no user input</div>
<h2>Recommendation</h2><div class=winner>Best for the current processes: <b>{esc(best['name'])}</b> (score {best['score']}/100)<br>
<span style='font-size:15px'>{esc(note_for(best['name']))}</span></div>
<h2>System probe</h2><div class=card>{esc(sysinfo['os'])} | {esc(str(sysinfo['cpu']))} | {sysinfo['cores_phys']} cores / {sysinfo['cores_log']} threads @ {sysinfo['freq_mhz']:.0f} MHz | RAM {sysinfo['ram_gb']:.1f} GB<br>
CPU load {sysinfo['cpu_load']:.0f}% | Disk {sysinfo['disk_mbps']:.2f} MB/s | Net down {sysinfo['net_down_kbps']:.1f} KB/s, up {sysinfo['net_up_kbps']:.1f} KB/s<br>
Measured context-switch latency {sysinfo['cs_us']:.1f} us | Compute speed {sysinfo['mops']:.1f} Mops/s | RR quantum {quantum} ms (80th percentile of bursts)</div>
<h2>Workload (live processes)</h2><div class=card><table><tr><th>PID</th><th>Process</th><th>Arrival ms</th><th>Burst ms</th><th>Priority</th><th>Mcycles</th><th>Type</th></tr>{rows}</table>
<p class=small>Burst CV {profile['cv']:.2f}, I/O-bound share {profile['io_share']*100:.0f}%</p></div>
<h2>Results</h2><div class=card><table><tr><th>#</th><th>Algorithm</th><th>Avg wait</th><th>Avg TAT</th><th>Avg resp</th><th>Max wait</th><th>Ctx sw</th><th>CPU %</th><th>Fairness</th><th>Score</th></tr>{res}</table></div>
<h2>Score</h2><div class=card>{bars}</div>
<h2>Gantt charts</h2><div class=card>{legend}{gantt}</div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def recommend(results, profile):
    """Turn the ranked results into a human-readable recommendation (used by the website)."""
    best = results[0]
    champs = {}
    for key, label, low in (("wait", "lowest average waiting time", True), ("tat", "lowest average turnaround time", True),
                            ("resp", "best average response time", True), ("maxwait", "lowest worst-case wait (least starvation)", True),
                            ("switches", "fewest context switches", True), ("fair", "fairest treatment of processes", False)):
        champ = min(results, key=lambda r: r[key]) if low else max(results, key=lambda r: r[key])
        champs[key] = champ["name"]
    why = []
    labels = {"wait": "lowest average waiting time", "tat": "lowest average turnaround time", "resp": "best average response time",
              "maxwait": "lowest worst-case wait (least starvation)", "switches": "fewest context switches",
              "fair": "fairest treatment of processes"}
    for key, name in champs.items():
        if name == best["name"] and key != "switches":
            why.append(labels[key])
    if not why:
        why = ["best overall balance across all metrics"]
    workload = ("Burst times are " + ("very uneven (high variation), so waiting time and starvation were weighted more. "
                if profile["cv"] > 1 else "fairly even. ") +
                ("Many processes are I/O-heavy/interactive, so response time was weighted more." if profile["io_share"] > 0.4
                 else "The workload is mostly CPU-bound."))
    return {"best": best["name"], "score": best["score"], "runner_up": results[1]["name"], "runner_score": results[1]["score"],
            "why": why, "note": note_for(best["name"]), "workload": workload, "best_by": champs}


def run_analysis(n=12):
    """Probe the machine, build a workload from live processes, simulate every algorithm, score and rank them."""
    sysinfo = probe_system()
    procs = build_workload(sysinfo["freq_mhz"], n=n)
    bursts = sorted(p["burst"] for p in procs)
    quantum = max(4, bursts[min(len(bursts) - 1, int(0.8 * len(bursts)))])
    profile = {"cv": statistics.pstdev(bursts) / statistics.mean(bursts),
               "io_share": sum(p["kind"] == "I/O-bound" for p in procs) / len(procs)}
    cs_ms = sysinfo["cs_us"] / 1000.0
    policies = [
        Keyed("FCFS", lambda p: p["arrival"], False),
        Keyed("SJF", lambda p: p["burst"], False),
        Keyed("SRTF", lambda p: p["rem"], True),
        Keyed("Priority (non-preemptive)", lambda p: p["priority"], False),
        Keyed("Priority (preemptive)", lambda p: p["priority"], True),
        RR(quantum),
        MLFQ(quantum),
    ]
    results = [simulate(procs, pol, cs_ms) for pol in policies]
    weights = score(results, profile)
    return {"sysinfo": sysinfo, "procs": procs, "results": results, "profile": profile,
            "weights": weights, "quantum": quantum, "cs_ms": cs_ms,
            "recommendation": recommend(results, profile)}


def main():
    ap = argparse.ArgumentParser(description="Auto CPU scheduling analyzer (no input required)")
    ap.add_argument("--n", type=int, default=12, help="number of live processes to use")
    ap.add_argument("--out", default="scheduler_report.html")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    print("Probing system and sampling live processes (about 2 seconds)...")
    d = run_analysis(a.n)
    terminal_report(d["sysinfo"], d["procs"], d["results"], d["profile"], d["weights"], d["quantum"])
    html_report(a.out, d["sysinfo"], d["procs"], d["results"], d["profile"], d["quantum"])
    path = os.path.abspath(a.out)
    print(f" HTML report with Gantt charts saved to: {path}")
    if not a.no_browser:
        webbrowser.open("file://" + path)


if __name__ == "__main__":
    main()
