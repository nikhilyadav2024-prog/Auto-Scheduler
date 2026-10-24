# Auto CPU Scheduling Analyzer

**Operating Systems course project.** A program that measures your own computer, builds a workload from the processes that are actually running, simulates every major CPU scheduling algorithm on that workload, and tells you which algorithm is best for those processes. **No manual input of burst time, arrival time or anything else.**

---

## 1. What is in this folder

```
os_scheduler_project/
├── os_scheduler_analyzer.py   # the brain: probing, workload builder, 7 schedulers, scoring, CLI
├── server.py                  # web server (standard library only) that exposes the brain as an API
├── static/index.html          # the website / dashboard (HTML + CSS + JavaScript, works offline)
├── requirements.txt           # only one dependency: psutil
├── run.bat                    # Windows one-click start
├── run.sh                     # macOS / Linux one-click start
└── README.md                  # this file
```

---

## 2. How to run it on your laptop

### Step 0: Check Python
Open a terminal (Windows: *Command Prompt* or *PowerShell*; macOS/Linux: *Terminal*) and run:

```
python --version
```
You need **Python 3.8 or newer**. On macOS/Linux the command may be `python3`. If Python is missing, install it from https://www.python.org/downloads/ (on Windows tick **"Add Python to PATH"** during installation).

### Step 1: Put the folder somewhere
Copy the whole `os_scheduler_project` folder to your laptop (for example, the Desktop) and open a terminal inside it:

```
cd Desktop/os_scheduler_project
```

### Step 2: Install the only dependency
```
pip install -r requirements.txt
```
(macOS/Linux: `pip3 install -r requirements.txt`)

### Step 3: Start the website
```
python server.py
```
Your browser opens **http://127.0.0.1:8000** automatically. If it does not, open that address yourself. The first analysis starts on its own and takes about 3 seconds.

**Shortcut:** on Windows double-click `run.bat`; on macOS/Linux run `./run.sh`. Both do steps 2 and 3.

### Step 4: Use it
* Click **Analyze now** to re-measure and re-simulate with the processes running at that moment.
* Choose **Auto-refresh** (15/30/60 s) to watch the recommendation change live as your workload changes.
* Open a few apps (browser, video, IDE) or close them and analyze again: the recommended algorithm can change.
* Press **Ctrl + C** in the terminal to stop the server.

### Optional: terminal-only version (no website)
```
python os_scheduler_analyzer.py
```
Prints the full report in the terminal and also saves `scheduler_report.html` (with Gantt charts) and opens it.

### Options
| Command | Effect |
|---|---|
| `python server.py --port 9000` | use another port |
| `python server.py --no-browser` | do not auto-open the browser |
| `python os_scheduler_analyzer.py --n 16` | use 16 processes instead of 12 |

### Troubleshooting
| Problem | Fix |
|---|---|
| `No module named psutil` | run `pip install psutil` |
| `pip` not found | use `python -m pip install psutil` |
| Port already in use | `python server.py --port 9001` |
| "Not enough active processes" | open a few apps and try again |
| Some process names missing on Windows/macOS | normal: protected system processes deny access and are skipped |
| Disk/network show 0 | nothing was being transferred during the 1-second measurement window |

---

## 3. The idea in plain words

Textbook scheduling programs ask you to type arrival times and burst times. This project removes that: **your computer already has real processes with real histories**, so the program reads them and uses them as the test data. It then answers the question every OS student is asked: *"Which scheduling algorithm is best?"* The honest answer is *"it depends on the workload"*, and this tool shows exactly that, because the winner changes when your workload changes.

---

## 4. How it works, step by step

### 4.1 System probe (`probe_system`)
| Measured | How | Why it matters |
|---|---|---|
| CPU clock speed | `psutil.cpu_freq()` | converts each process's CPU seconds into CPU work in million cycles |
| Cores / threads, RAM, current CPU load | `psutil` | describes the machine |
| Disk throughput (MB/s) and network download/upload (KB/s) | counters read twice, 1 second apart | shows the I/O pressure on the system |
| Context-switch time | two threads ping-pong a byte through pipes thousands of times; time ÷ switches | **real switching cost**, charged to every algorithm in the simulation |
| Compute speed | timed loop of 2 million operations | machine speed indicator |

### 4.2 Build the workload (`build_workload`)
Two CPU-time snapshots of every process are taken one second apart. The most CPU-active processes (default 12) become the jobs.

| Scheduling input | Taken from the real process | Rule |
|---|---|---|
| **Arrival time** | process creation time | oldest process = 0 ms; the others are placed proportionally, stretched over 60% of the total work so that jobs overlap |
| **Burst time** | CPU time the process has consumed (user + system) | log-scaled into 5-100 ms: `burst = 5 + 95 * ln(1+cpu) / ln(1+max_cpu)`. Order is preserved, the simulation stays readable |
| **Priority** | OS *nice* value (Linux/macOS) or priority class (Windows) | `priority = nice + 20` (0 = most important); I/O-bound processes get a -3 interactive boost |
| **CPU work (Mcycles)** | CPU seconds × clock speed (MHz) | shows how much real computation the job represents |
| **CPU-bound / I/O-bound** | process disk I/O bytes ÷ CPU seconds | more than 5 MB per CPU-second = I/O-bound |
| **Round-robin quantum** | 80th percentile of the burst times | the textbook rule: about 80% of bursts should finish within one quantum |

### 4.3 The simulation engine (`simulate`)
* Time moves in **1 ms ticks**. At each tick the active policy chooses which ready process runs.
* When the CPU switches to a different process, the **measured context-switch time** is added to the clock (so algorithms that switch a lot are properly penalised).
* If no process is ready, the CPU idles until the next arrival.
* All algorithms see the **identical workload**, so the comparison is fair.

### 4.4 The seven algorithms
| Algorithm | Picks next | Preemptive? | Strength | Weakness |
|---|---|---|---|---|
| FCFS | earliest arrival | No | simple, no starvation | convoy effect: short jobs wait behind long ones |
| SJF | smallest burst | No | low average waiting | needs burst prediction; long jobs can starve |
| SRTF | smallest remaining time | Yes | optimal average waiting | many switches; starvation of long jobs |
| Priority (non-preemptive) | best priority number | No | respects importance | low priority can starve (no aging) |
| Priority (preemptive) | best priority number | Yes | urgent jobs run immediately | starvation, more switches |
| Round Robin | queue order, fixed quantum | Yes | fair, good response | more switches, longer turnaround |
| MLFQ | 3 queues (slices q/2, q, 2q); new jobs on top, demoted as they use CPU; higher queue preempts | Yes | approximates SJF without knowing bursts | complex; bottom queue can starve |

### 4.5 Metrics collected for each algorithm
* **Waiting time** = turnaround - burst (time spent ready but not running)
* **Turnaround time** = finish - arrival
* **Response time** = first run - arrival
* **Worst-case wait** = the largest waiting time (starvation indicator)
* **Context switches** = number of times the CPU changed process
* **CPU utilisation** = useful busy time ÷ total time
* **Throughput** = processes completed per second
* **Fairness (Jain's index)** over each process's slowdown (`turnaround / burst`): `J = (Σx)² / (n · Σx²)`, 1.0 means everyone is treated equally

### 4.6 Scoring and the recommendation (`score`, `recommend`)
1. Each metric is normalised between the best and worst algorithm: 0 = best, 1 = worst.
2. Each normalised value is multiplied by a weight and the total is subtracted from 100.

| Metric | Default weight |
|---|---|
| Average waiting time | 30% |
| Average response time | 20% |
| Fairness | 20% |
| Average turnaround time | 15% |
| Worst-case wait | 10% |
| Context switches | 5% |

3. **Adaptive weights:** if more than 40% of the processes are I/O-bound (interactive), response time gains +10% (taken from waiting and turnaround). If burst times vary a lot (coefficient of variation above 1), waiting time and worst-case wait gain +5% each (taken from turnaround and fairness).
4. The highest score is the **recommendation**. The tool also lists *why* it won (which metrics it leads), its known trade-offs and the runner-up.

---

## 5. The website

`static/index.html` is a single file with no external libraries, so it works offline. It calls `/api/analyze` and draws:
* **Winner banner** with reasons and trade-offs
* **System cards** (clock, cores, RAM, load, disk, network, measured context switch, compute speed, quantum)
* **Workload table** (each process with arrival, burst, priority, Mcycles, type)
* **Ranking table** and **score bars**
* **Metric comparison** with tabs (waiting, turnaround, response, worst-case wait, switches, fairness)
* **Gantt charts** for all seven algorithms on a shared time axis
* **Scoring weights** actually used for this run
* Light/dark mode follows your system, layout works on phone widths too

### API
| Route | Returns |
|---|---|
| `GET /` | the dashboard |
| `GET /api/analyze?n=12` | JSON: `sysinfo`, `procs`, `results` (with Gantt timelines), `profile`, `weights`, `quantum`, `recommendation` |
| `GET /api/health` | `{"status": "ok"}` |

---

## 6. Reading a result (example)

> Recommended: **SRTF**, score 99.5/100. Lowest waiting, turnaround and response time and fairest. Burst times fairly even, CPU-bound workload. Runner-up: SJF.

Meaning: the processes had short and long jobs mixed, so always running the job with the least time left minimised waiting for everyone. On a laptop full of interactive apps (browser, chat, editor) you may instead see MLFQ or Round Robin rise, because response time then carries more weight.

---

## 7. Design decisions you can defend in the review

* **Why simulate instead of changing the real OS scheduler?** The real scheduler cannot be swapped from user space and doing so would be unsafe. A simulation on real data lets us compare all algorithms on identical input.
* **Why are bursts scaled?** Lifetime CPU seconds range from milliseconds to hours. Log scaling keeps the real ordering but makes a readable 5-100 ms simulation.
* **SJF and SRTF need to know the burst in advance. How is that realistic?** A real OS predicts it using exponential averaging of past bursts. Here the recorded CPU history plays that role. Their starvation risk is shown by the worst-case wait metric.
* **Why measure context-switch time?** The textbook assumes switching is free. Measuring it on the actual machine makes the comparison realistic.
* **Why a composite score?** No single metric is "best". The weighted score balances speed, responsiveness, fairness and overhead, and the weights adapt to the workload type.

## 8. Limitations and future work
* Snapshot-based: jobs are single CPU bursts, not repeated CPU/I/O cycles.
* Single-CPU model (multi-core load balancing is not simulated).
* Priority algorithms have no aging; MLFQ has no priority boost.
* Ideas: exponential-averaging burst predictor, aging, multi-core simulation, history chart of recommendations over time, CFS (Linux) as an eighth algorithm.

## 9. Likely viva questions
1. *Difference between waiting, turnaround and response time?* See section 4.5.
2. *What is the convoy effect?* A long job at the front makes all short jobs wait (FCFS).
3. *Why can SJF starve?* Short jobs keep arriving and the long job never gets chosen.
4. *Why does Round Robin have more context switches?* Every quantum expiry forces a switch.
5. *What does the quantum do?* Too large behaves like FCFS; too small wastes time on switching.
6. *How does MLFQ work without knowing burst lengths?* It learns: jobs that use their whole slice are demoted, short interactive jobs stay on top.
7. *Where do the numbers come from?* From the machine and live processes, section 4.1 and 4.2.
