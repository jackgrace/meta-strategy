# Meta Ad Fatigue Agent

DTC skincare business running ASC (Advantage+ Shopping) campaigns across AU, UK, US, CA, EU, UAE.
Single ASC campaign per market. No stop-loss rules. Slow titration approach to scaling spend.

## Core philosophy
- Fatigue is a TREND, not a snapshot. An ad with 1.2x ROAS that's stable is fine. An ad that dropped from 3.5x to 1.8x over 5 days is fatiguing.
- Ads with low ROAS but high CTR/low CPC are doing their job — ASC is choosing to spend on them for a reason. Don't flag these as underperforming.
- Every ad is scored against its OWN baseline (first 4 days of 7-day window vs last 3 days). No arbitrary hard thresholds.
- Two ad roles: "efficiency" (high ROAS, bottom-funnel) weighted on ROAS decay, and "engagement" (low ROAS, strong CTR/CPC) weighted on CTR decay and CPC inflation.

## Stack
- Python 3.12, minimal dependencies (just requests)
- Meta Marketing API for ad-level insights
- Slack incoming webhook for daily digest
- Railway cron job (midnight AEST = 0 14 * * * UTC)

## Deploy target
Railway via Dockerfile + railway.toml cron schedule.
