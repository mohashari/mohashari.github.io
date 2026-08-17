---
layout: post
title: "A Statistical Model for Estimating Developer Fatigue from On-Call Alert Noise and Interrupted Sleep Cycles"
date: 2026-08-17 08:00:00 +0700
tags: [on-call, sre, engineering-management, telemetry]
description: "A mathematical framework and production pipeline to quantify developer fatigue, prevent burnout, and dynamically adjust on-call rotations using real-time telemetry."
image: "/images/diagrams/statistical-model-estimating-developer-fatigue-on-call-alert-noise-interrupted-sleep.svg"
thumbnail: "/images/diagrams/statistical-model-estimating-developer-fatigue-on-call-alert-noise-interrupted-sleep.svg"
---

A 3:00 AM page about a transient CPU spike or a database connection pool blip is far more than a 15-minute inconvenience; it is a cognitive tax that degrades engineering execution for the next 24 to 48 hours. When a primary on-call engineer is woken up multiple times in a single night, the resulting sleep fragmentation destroys their ability to write safe code, debug complex production incidents, or make rational architectural decisions during their regular shift. Yet, engineering organizations continue to treat on-call shifts as binary states: either you are on-call and expected to perform at 100% capacity, or you are off-call. They ignore the cumulative, non-linear degradation of human performance caused by alert noise and interrupted sleep cycles. This post presents a mathematically rigorous, telemetry-driven statistical model to quantify developer fatigue in real-time, pulling alert signals directly from systems like PagerDuty or Opsgenie and combining them with circadian sleep-cycle heuristics. By calculating a dynamic Fatigue Index, engineering organizations can transition from static, rigid schedules to adaptive, closed-loop on-call systems that automatically swap exhausted engineers, prevent burnout before it happens, and enforce operational accountability for flaky alerts.

![A Statistical Model for Estimating Developer Fatigue from On-Call Alert Noise and Interrupted Sleep Cycles Diagram](/images/diagrams/statistical-model-estimating-developer-fatigue-on-call-alert-noise-interrupted-sleep.svg)

## The Human Cost of On-Call: Sleep Fragmentation and Cognitive Decay

To build an accurate mathematical model of fatigue, we must first understand the biological constraints of human sleep. Human sleep is not a uniform block of unconsciousness; it is structured in 90-minute ultradian cycles consisting of Non-Rapid Eye Movement (NREM) and Rapid Eye Movement (REM) sleep. NREM sleep, specifically Stage 3 and Stage 4 slow-wave sleep, is critical for physical recovery and clearing metabolic waste from the brain. REM sleep is essential for cognitive processing, emotional regulation, and decision-making. 

When a developer is interrupted by a high-urgency pager notification, two critical biological phenomena occur:

1. **Sleep Inertia**: Upon waking abruptly from deep slow-wave sleep, individuals experience a temporary period of cognitive impairment, slow reaction times, and grogginess. Sleep inertia can last anywhere from 15 minutes to 4 hours. During this period, the engineer is operating at a cognitive deficit equivalent to mild alcohol intoxication, significantly increasing the risk of making mistakes while trying to resolve the incident.
2. **REM Deprivation**: Because REM sleep periods become progressively longer in the latter half of an 8-hour sleep cycle, waking up early in the morning (e.g., at 4:30 AM or 6:00 AM) selectively deprives the developer of REM sleep. This leads to impaired analytical skills, reduced working memory, and increased irritability the following day.

The primary failure mode of standard on-call metrics (like PagerDuty's "Time to Acknowledge" or "Time to Resolve") is that they measure system activity rather than human impact. An alert that triggers at 3:00 AM and is resolved in 5 minutes is logged by the platform as 5 minutes of work. In reality, that alert has shattered a sleep cycle. The developer may take another 45 minutes to fall back asleep, and the interruption has reset their sleep architecture. If this occurs multiple times a night, the developer’s cognitive capacity is degraded, even if the total "active incident time" is less than 30 minutes. We must construct a model that treats these sleep interruptions as non-linear, cumulative penalties that decay slowly over time.

## Formulating the Statistical Fatigue Model

We represent the developer’s Fatigue Index, $F(t) \in [0, 1]$, as a continuous-time state variable that accumulates with alerts and sleep interruptions, and decays during rest periods. The total fatigue is composed of two primary mathematical components: the **Acute Alert Load** ($A(t)$) and the **Circadian Sleep Interruption Penalty** ($S(t)$).

### 1. Acute Alert Load ($A(t)$)

The Acute Alert Load models the immediate stress and cognitive load of dealing with incidents. Each alert event $i$ occurring at time $t_i$ injects a shock into the developer's cognitive state. This shock decays exponentially over time as the developer recovers from the incident:

$$A(t) = \sum_{i} w_i \cdot e^{-\lambda_a (t - t_i)}$$

Where:
* $t_i$ is the timestamp when alert $i$ was triggered.
* $w_i$ is the weight of the alert, representing its cognitive severity. A SEV1 page that requires immediate active debugging has $w_i = 1.0$, while a low-urgency Slack notification that still sends a push notification during daytime hours might have $w_i = 0.15$.
* $\lambda_a$ is the decay rate of acute stress. We tune this using the half-life of acute stress recovery, $H_a$. If we assume that the immediate stress of an incident resolves with a half-life of 2.5 hours ($H_a = 2.5$), then the decay constant is:

$$\lambda_a = \frac{\ln(2)}{H_a} \approx 0.277 \text{ hour}^{-1}$$

### 2. Circadian Sleep Interruption Penalty ($S(t)$)

The Sleep Interruption Penalty represents the deeper, cumulative sleep debt incurred when alerts occur during the developer's designated sleep window. Let $W_{\text{sleep}} = [t_{\text{sleep\_start}}, t_{\text{sleep\_end}}]$ be the developer's regular sleep window (e.g., 23:00 to 07:00 local time).

If an alert $j$ falls within $W_{\text{sleep}}$, it triggers a circadian penalty $P_j$. Unlike acute stress, sleep debt decays much more slowly, requiring a full night of uninterrupted sleep to clear. We model this recovery with a much lower decay rate $\lambda_s$:

$$S(t) = \sum_{j} P_j \cdot e^{-\lambda_s (t - t_j)}$$

Where:
* $t_j$ is the timestamp of the sleep interruption.
* $\lambda_s$ is the decay rate of sleep debt, tuned to a half-life $H_s$ of 24 hours ($H_s = 24.0$), representing the fact that recovery from a fragmented sleep cycle takes at least a full day:

$$\lambda_s = \frac{\ln(2)}{H_s} \approx 0.0289 \text{ hour}^{-1}$$

* $P_j$ is the calculated penalty for interruption $j$. To model the non-linear impact of multiple wake-ups, the penalty is scaled by the number of interruptions within the same night and the duration the developer was kept awake:

$$P_j = \text{base\_penalty} \cdot (1 + \mu \cdot \Delta t_{\text{active}}) \cdot \gamma^{\text{count}}$$

Where:
* $\text{base\_penalty}$ is set to $1.0$ for high-urgency alerts during sleep windows.
* $\Delta t_{\text{active}}$ is the time in hours between the alert trigger and resolution. If an incident takes 1.5 hours to resolve, the developer was awake longer, multiplying the circadian disruption.
* $\mu$ is a scaling factor for active time (e.g., $\mu = 0.5$).
* $\text{count}$ is the number of sleep-window interruptions that have occurred in the current 24-hour cycle. 
* $\gamma$ is a compounding factor (e.g., $\gamma = 1.5$). A second interruption in the same night is $1.5\times$ more damaging than the first, and a third is $2.25\times$ more damaging, capturing the catastrophic impact of sleep fragmentation.

### 3. Combining into the Fatigue Index ($F(t)$)

To combine the acute load and sleep debt into a normalized index between 0.0 (completely rested) and 1.0 (critically exhausted), we apply a logistic or hyperbolic tangent activation function:

$$F(t) = \tanh\left( \alpha \cdot A(t) + \beta \cdot S(t) \right)$$

Where $\alpha$ and $\beta$ are scaling coefficients used to calibrate the sensitivity of the index. In practice, we set $\alpha = 0.4$ and $\beta = 0.6$, placing a higher weight on sleep disruption than on daytime alert volume.

## Designing the Closed-Loop Telemetry Pipeline

To make this model actionable, we cannot rely on manual data entry. We need a closed-loop system that automatically ingests alert events, updates the state in a fast database, and executes schedule changes when fatigue thresholds are breached.

The ingestion pipeline consists of four main stages:

1. **Webhook Ingestion**: PagerDuty, Opsgenie, or Prometheus Alertmanager emits webhook payloads to our ingestion service when alerts are `triggered`, `acknowledged`, or `resolved`.
2. **User Registry Resolution**: The ingestion service extracts the target user's PagerDuty ID and queries an engineering registry (like Spotify Backstage or a DynamoDB database) to resolve their local timezone, regular working hours, and typical sleep window.
3. **State Updates in Redis**: Because the model relies on historical event streams to compute the decay, we store the active alert list for each engineer in Redis. When a webhook arrives, the system recalculates the current values of $A(t)$ and $S(t)$ and writes them back.
4. **Actionable Mitigation**: A worker process evaluates the Fatigue Index $F(t)$ every 5 minutes. If an engineer's $F(t)$ crosses a critical threshold (e.g., $F(t) \ge 0.8$), the mitigation engine invokes the PagerDuty REST API to create a temporary schedule override. The primary on-call engineer is swapped out, and the secondary engineer is promoted to primary for the remainder of the sleep cycle or shift.

## Implementing the Fatigue Engine in Python

Below is a production-grade Python implementation of the mathematical model. It utilizes `datetime` with timezone localization via `pytz` to accurately evaluate whether alerts fall within an engineer's sleep window, applies the exponential decay formulas, and returns the compiled Fatigue Index.

```python
from datetime import datetime, timedelta
import math
from typing import List, Dict, Any
import pytz

class AlertEvent:
    def __init__(self, trigger_time: datetime, resolve_time: datetime, severity: str, is_sleep_interrupted: bool = False):
        """
        Represents an individual alert event.
        All times must be timezone-aware (UTC recommended).
        """
        self.trigger_time = trigger_time
        self.resolve_time = resolve_time
        self.severity = severity.upper()
        self.is_sleep_interrupted = is_sleep_interrupted

    @property
    def active_duration_hours(self) -> float:
        duration = self.resolve_time - self.trigger_time
        return max(0.0, duration.total_seconds() / 3600.0)

class DeveloperProfile:
    def __init__(self, developer_id: str, timezone_str: str, sleep_start_hour: int = 23, sleep_end_hour: int = 7):
        self.developer_id = developer_id
        self.timezone = pytz.timezone(timezone_str)
        self.sleep_start_hour = sleep_start_hour
        self.sleep_end_hour = sleep_end_hour

    def is_within_sleep_window(self, dt: datetime) -> bool:
        """Checks if a given UTC datetime falls within the developer's local sleep window."""
        local_dt = dt.astimezone(self.timezone)
        hour = local_dt.hour
        if self.sleep_start_hour > self.sleep_end_hour:
            # Over-midnight window (e.g., 23:00 to 07:00)
            return hour >= self.sleep_start_hour or hour < self.sleep_end_hour
        else:
            # Daytime sleep window (e.g., night shift worker, 08:00 to 16:00)
            return self.sleep_start_hour <= hour < self.sleep_end_hour

class FatigueEvaluator:
    def __init__(self, 
                 lambda_a_half_life: float = 2.5,  # Hours to decay half of acute stress
                 lambda_s_half_life: float = 24.0, # Hours to decay half of sleep debt
                 alpha: float = 0.4,               # Weight coefficient for acute load
                 beta: float = 0.6,                # Weight coefficient for sleep debt
                 active_time_weight: float = 0.5,  # Scaling factor for duration awake
                 compounding_factor: float = 1.5): # Compounding factor for multiple wake-ups
        self.lambda_a = math.log(2) / lambda_a_half_life
        self.lambda_s = math.log(2) / lambda_s_half_life
        self.alpha = alpha
        self.beta = beta
        self.mu = active_time_weight
        self.gamma = compounding_factor

    def evaluate_fatigue(self, profile: DeveloperProfile, alerts: List[AlertEvent], evaluation_time: datetime) -> Dict[str, Any]:
        """
        Calculates the acute alert load, sleep interruption penalty, and final Fatigue Index.
        """
        acute_load = 0.0
        sleep_penalty = 0.0
        
        # We group sleep interruptions by 'sleep period' (defined as noon-to-noon local time)
        # to apply compounding penalties for multiple wake-ups within the same night.
        sleep_periods: Dict[str, List[AlertEvent]] = {}

        for alert in alerts:
            # Skip alerts that occur after the evaluation timestamp
            if alert.trigger_time > evaluation_time:
                continue

            # 1. Compute Acute Alert Load Contribution
            # Map alert severity to numerical weight
            severity_weights = {"CRITICAL": 1.0, "HIGH": 0.8, "MEDIUM": 0.4, "LOW": 0.15}
            w_i = severity_weights.get(alert.severity, 0.2)
            
            # Time elapsed since the alert triggered
            delta_t_acute = (evaluation_time - alert.trigger_time).total_seconds() / 3600.0
            acute_load += w_i * math.exp(-self.lambda_a * delta_t_acute)

            # 2. Check for Sleep Interruption
            if profile.is_within_sleep_window(alert.trigger_time):
                alert.is_sleep_interrupted = True
                
                # Determine the local sleep period identifier (YYYY-MM-DD representing the date at start of sleep)
                local_time = alert.trigger_time.astimezone(profile.timezone)
                if local_time.hour < profile.sleep_end_hour:
                    # If it's early morning, it belongs to the previous day's sleep window starting night before
                    sleep_date = (local_time - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    sleep_date = local_time.strftime("%Y-%m-%d")
                
                if sleep_date not in sleep_periods:
                    sleep_periods[sleep_date] = []
                sleep_periods[sleep_date].append(alert)

        # 3. Calculate Cumulative Sleep Interruption Penalty
        for sleep_date, period_alerts in sleep_periods.items():
            # Sort alerts in chronological order for this night
            period_alerts.sort(key=lambda x: x.trigger_time)
            
            for idx, alert in enumerate(period_alerts):
                # Count represents the number of prior interruptions in the same sleep block
                count = idx 
                base_p = 1.0 if alert.severity in ["CRITICAL", "HIGH"] else 0.4
                
                # Compute raw penalty for this wake-up event
                p_j = base_p * (1.0 + (self.mu * alert.active_duration_hours)) * (self.gamma ** count)
                
                # Time elapsed since sleep interruption
                delta_t_sleep = (evaluation_time - alert.trigger_time).total_seconds() / 3600.0
                sleep_penalty += p_j * math.exp(-self.lambda_s * delta_t_sleep)

        # 4. Compute combined Fatigue Index using hyperbolic tangent
        raw_score = (self.alpha * acute_load) + (self.beta * sleep_penalty)
        fatigue_index = math.tanh(raw_score)

        return {
            "developer_id": profile.developer_id,
            "evaluation_time_utc": evaluation_time.isoformat(),
            "acute_load": round(acute_load, 4),
            "sleep_penalty": round(sleep_penalty, 4),
            "fatigue_index": round(fatigue_index, 4),
            "swap_recommended": fatigue_index >= 0.80
        }

# Example Usage
if __name__ == "__main__":
    # Developer based in Jakarta (UTC+7)
    dev_profile = DeveloperProfile(developer_id="m-ashari-muklis", timezone_str="Asia/Jakarta")
    evaluator = FatigueEvaluator()

    # Current evaluation time set to 2026-08-17 08:30:00 UTC (15:30:00 Local Jakarta Time)
    evaluation_ts = datetime(2026, 8, 17, 8, 30, 0, tzinfo=pytz.UTC)

    # Let's simulate a bad night:
    # Alert 1: 03:00 AM local time (20:00 UTC Aug 16), critical, resolved in 30 mins
    # Alert 2: 04:30 AM local time (21:30 UTC Aug 16), critical, resolved in 45 mins
    simulated_alerts = [
        AlertEvent(
            trigger_time=datetime(2026, 8, 16, 20, 0, 0, tzinfo=pytz.UTC),
            resolve_time=datetime(2026, 8, 16, 20, 30, 0, tzinfo=pytz.UTC),
            severity="CRITICAL"
        ),
        AlertEvent(
            trigger_time=datetime(2026, 8, 16, 21, 30, 0, tzinfo=pytz.UTC),
            resolve_time=datetime(2026, 8, 16, 22, 15, 0, tzinfo=pytz.UTC),
            severity="CRITICAL"
        )
    ]

    metrics = evaluator.evaluate_fatigue(dev_profile, simulated_alerts, evaluation_ts)
    print(f"Results for Developer: {metrics['developer_id']}")
    print(f"Fatigue Index: {metrics['fatigue_index']}")
    print(f"Acute Load Metric: {metrics['acute_load']}")
    print(f"Sleep Penalty Metric: {metrics['sleep_penalty']}")
    print(f"Recommend Shift Swap: {metrics['swap_recommended']}")
```

## Production Failure Modes and Edge Cases

Running an automated on-call routing pipeline introduces real-world edge cases that will cause operational issues if not proactively managed.

### 1. Alert Storming vs. Sleep Fragmentation

During a major system outage, Prometheus Alertmanager might fire 150 separate alerts in the span of 20 minutes. If the model treats every single alert event as a separate sleep interruption, the engineer’s sleep penalty will instantly saturate to $1.0$, triggering an immediate override swap. 
* **The Failure Mode**: The primary engineer is actively debugging the incident and should not be swapped out in the middle of resolving a critical outage. Furthermore, they were woken up once, not 150 times.
* **The Mitigation**: The ingestion pipeline must implement a sliding deduplication window (e.g., 30 minutes). Any alerts that trigger for the same engineer within 30 minutes of an active alert are grouped under a single "Sleep Interruption Event." The duration of this grouped event spans from the first trigger to the final resolve timestamp.

### 2. Timezone Drift and Remote Work

If an engineer based in Jakarta (`Asia/Jakarta`) travels to London (`Europe/London`) for a conference but their profile is not updated in the user registry, the pipeline will calculate their sleep interruptions based on Jakarta time.
* **The Failure Mode**: The engineer is paged at 3:00 PM local London time (which is 9:00 PM Jakarta time). The system evaluates this as outside their sleep window. Later, they get paged at 10:00 PM London time (4:00 AM Jakarta time). The system flags this as a critical sleep interruption, when in reality the engineer is awake.
* **The Mitigation**: The user registry must dynamically sync timezone data. Integrate the system with Slack's `users.info` API to poll the engineer's current active client timezone offset daily, or query calendar/HR systems (like Workday or BambooHR) to automatically update timezone fields when travel statuses change.

### 3. The "Hero" Anti-Pattern and Tragedy of the Commons

Engineers are notoriously resistant to being swapped out. A common behavioral pattern is for the primary on-call to manually delete automated schedule overrides because they feel they can "tough it out" or do not want to burden the secondary engineer.
* **The Failure Mode**: By overriding the safety mechanism, the fatigued engineer remains on-call, defeats the purpose of the control loop, and eventually makes an operational error during their regular working shift.
* **The Mitigation**: Enforce hard programmatic boundaries in PagerDuty schedule configuration. Do not grant write permissions on schedule overrides to the on-call engineers themselves. Overrides must be managed solely by the automation engine's API key. If an engineer attempts to bypass the override, the system automatically alerts the engineering manager and creates an audit trail.

## Organizational Governance and Operational Accountability

Implementing a statistical fatigue model is not just a technical exercise; it is an organizational tool to enforce operational quality. To drive real behavioral change, you must tie fatigue metrics to your engineering KPIs.

### The "Fatigue Budget"

Similar to Site Reliability Engineering's (SRE) concept of an Error Budget, teams should establish a **Fatigue Budget**. A typical Fatigue Budget allocates a maximum average team Fatigue Index of $0.35$ over a 14-day sprint. 

* If the team's average Fatigue Index remains below the threshold, feature development proceeds as planned.
* If a series of bad nights causes the team's Fatigue Index to cross the $0.35$ threshold, the team's "Fatigue Budget" is exhausted. The engineering manager must immediately halt feature work for the next sprint and redirect the team's resources toward platform reliability: refactoring flaky alerts, fixing flapping checks, tuning auto-scaler parameters, and writing self-healing runbooks.

This framework aligns developers, SREs, and product managers by ensuring that when operational noise becomes high enough to cause human degradation, the product roadmap slows down to address the root systemic causes. Without a quantitative model like the one detailed here, fatigue remains an invisible, qualitative complaint that is easily ignored. By establishing a telemetry-driven Fatigue Index, you treat human engineers with the same telemetry, monitoring, and error-handling principles that you apply to production infrastructure.