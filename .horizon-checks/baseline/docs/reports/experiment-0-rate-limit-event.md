# Experiment 0: rate-limit events on setup-token Claude lanes

Run 2026-09-05 07:2x EDT by the planning session. Command per lane (token supplied through CLAUDE_CODE_OAUTH_TOKEN from the agent keychain, never printed):

```
claude -p "Reply with exactly: ok" --model claude-haiku-4-5-20251001 --output-format stream-json --verbose --max-turns 1
```

CLI: 2.1.260 (Claude Code). Both runs rc=0, subtype success, one turn, about 3 to 6 seconds. Event sequence per run: system/init, system/hook_started x2, system/hook_response x2, assistant x2, rate_limit_event, system/post_turn_summary, result/success.

## max@axiom.org

```json
{
 "type": "rate_limit_event",
 "rate_limit_info": {
  "status": "allowed",
  "resetsAt": 1788624000,
  "rateLimitType": "five_hour",
  "overageStatus": "rejected",
  "overageDisabledReason": "org_level_disabled",
  "isUsingOverage": false,
  "unifiedWindows": {
   "five_hour": {
    "utilization": 0.05,
    "resetsAt": 1788624000
   },
   "seven_day": {
    "utilization": 0.25,
    "resetsAt": 1789056000
   }
  }
 },
 "session_id": "281408bb-280e-43da-9455-b9f3ddebf275"
}
```

## max@thesisinstitute.org

```json
{
 "type": "rate_limit_event",
 "rate_limit_info": {
  "status": "allowed",
  "resetsAt": 1788612600,
  "rateLimitType": "five_hour",
  "overageStatus": "rejected",
  "overageDisabledReason": "out_of_credits",
  "isUsingOverage": false,
  "unifiedWindows": {
   "five_hour": {
    "utilization": 0.29,
    "resetsAt": 1788612600
   },
   "seven_day": {
    "utilization": 0.18,
    "resetsAt": 1788613200
   }
  }
 },
 "session_id": "bf80957d-887d-4cd8-a131-e70a97f14a54"
}
```

# Follow-up probes, 07:25 EDT

## max@policyengine.org, --model claude-fable-5-1 (lane carried a local Fable cooldown)

rc=1. The result was `is_error: true` with the text: You're out of usage credits. Switch to another model, or manage usage credits at claude.ai/settings/usage, to continue.

```json
{
 "type": "rate_limit_event",
 "rate_limit_info": {
  "status": "rejected",
  "resetsAt": 1790812800,
  "overageDisabledReason": "out_of_credits",
  "isUsingOverage": false,
  "errorCode": "credits_required",
  "canUserPurchaseCredits": true,
  "hasChargeableSavedPaymentMethod": true
 },
 "session_id": "fa8e5306-d775-4435-bd42-29f1396af991"
}
```

## max.ghenis@gmail.com, --model claude-haiku-4-5-20251001 (cached table: EXHAUSTED, 147% of the week)

rc=0, result success.

```json
{
 "type": "rate_limit_event",
 "rate_limit_info": {
  "status": "allowed",
  "resetsAt": 1788612600,
  "rateLimitType": "five_hour",
  "overageStatus": "rejected",
  "overageDisabledReason": "org_level_disabled",
  "isUsingOverage": false,
  "unifiedWindows": {
   "five_hour": {
    "utilization": 0.42,
    "resetsAt": 1788612600
   },
   "seven_day": {
    "utilization": 0.33,
    "resetsAt": 1788616800
   }
  }
 },
 "session_id": "cc1514f8-ebac-4d71-ba0e-356d663b8784"
}
```
