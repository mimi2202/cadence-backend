"""
Sets the real body_tpl and subject_tpl for the Singapore Micro1 campaign.
Run once.
"""
from database import execute

subject = "Quick question about {{ business }}"

body = """{{ opener }}

We're partnering with Micro1 AI as an independent data referral partner, connecting businesses with proven operations to a video data collection program for AI training.

Micro1 is looking for legacy businesses in select sectors to record everyday workflows — the day-to-day work that makes your business run. It pays up to $8/hour per hour of video submitted, and there's no cap on how much you can submit.

Disclosure: as a referral partner, I may earn a commission if your business signs up through this link.

More details here: https://req.micro1.ai/post/acd4e6e5-0afb-4b83-bcb0-fc6a81904063?referralCode=0a7fc627-eca8-4d00-a787-1f42efa7afca&utm_source=referral&utm_medium=share&utm_campaign=data_referral

{{ unsubscribe_url }}"""

n = execute(
    "UPDATE campaign SET subject_tpl=%s, body_tpl=%s WHERE id=%s",
    (subject, body, "b16ed0e8-d8cd-4292-80fe-c6b98d94f5c9"),
)
print(f"Rows updated: {n}")