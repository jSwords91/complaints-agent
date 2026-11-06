---
name: complaint-handling
description: How to handle any customer complaint about payments, charges or fees. Load before opening, resolving or escalating a case.
---

# Complaint handling

1. Verify the customer with `verify_customer` before looking at or changing anything.
   If verification fails, share nothing about the account. Ask them to check their details.
2. Find the evidence: `find_transactions` for payments, `get_customer_overview` for fee
   history and support needs.
3. Open one case per complaint, with the category that fits.
4. Decide using the policy below. Resolve it if it's within policy and your authority.
   Otherwise escalate it.
5. Reply in plain English: what you found, what you did, and what happens next.
   Don't promise an outcome you haven't delivered.

A general question, with nothing to fix, is not a complaint. Answer it without opening a case.

## Policy

| Situation | What to do |
| --- | --- |
| Two identical payments to one merchant on one day, and the customer only made one | Refund one payment as a goodwill credit (category `card_dispute`) |
| Unarranged overdraft fee, and no fee refunded in the last 12 months | Refund that fee once as goodwill (category `fees_and_charges`), and say it's a one-off |
| Goods or services not received, above your credit authority | Escalate with priority `normal` (category `card_dispute`) |
| Payments the customer doesn't recognise, or any sign of a scam | Load the `fraud-and-scams` skill |

UK complaint rules give us 8 weeks to send a final response. Mention it only if the case is escalated.
