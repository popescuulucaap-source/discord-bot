# Gargand moderation setup

## Anti-NSFW text and image filter

1. In the Fadehost bot service settings, add an environment variable named `OPENAI_API_KEY` or `OPENAI_MODERATION_API_KEY` and set its value to your own API key. Do not paste the key into Discord, GitHub files, or public chat.
2. Restart/redeploy the bot so the new environment variable is loaded.
3. In your Discord server, an administrator can run `!antinsfw status`, then `!antinsfw on`.
4. Use `!antinsfw off` to disable the filter again.

With the moderation key configured, Gargand submits message text, eligible image attachments, direct image links and Discord image previews (up to three images per message, 20 MB per attachment) to the `omni-moderation-latest` moderation endpoint. It removes content flagged by the sexual-content categories, attempts to notify the author privately, and logs an action in the configured anti-NSFW/Auto-Mod/general log channel. The moderation endpoint supports text and image inputs and requires a valid API key: https://developers.openai.com/api/docs/guides/moderation.

Without the key, only a small local text-pattern fallback is available; it does **not** visually inspect photographs. `!antinsfw status` explains whether a moderation key was detected. A detected key does not guarantee that the key is valid or that the external service is reachable.

Moderation classifiers can make mistakes and can miss content. Review the server's results and keep Discord's native safety controls enabled. The OpenAI moderation endpoint is not designed to detect or handle known or suspected child sexual abuse material; do not submit known or suspected material to that endpoint. Follow Discord's own reporting procedures for suspected exploitation.

## Private member reports

1. Create a private text channel such as `#reports`; allow only moderators/admins to view it.
2. Run `!reportchannel #reports`.
3. Members can use `!report @member reason`. The existing report command saves a moderation case and sends the report to the configured channel; if no reports channel is configured, it uses Gargand's normal logging route.

The bot needs permission to delete messages and send embeds/DMs for the anti-NSFW workflow, and permission to view/send messages in the private reports channel.

## Anti-phishing link protection

1. An administrator can run `!antiphishing status` to check the current state.
2. Run `!antiphishing on` to enable it, or `!antiphishing off` to disable it. The matching `/antiphishing` slash command is also available.
3. Gargand removes links matching its high-confidence suspicious-host, fake Discord/Nitro, URL-credential, raw-IP or punycode checks, privately notifies the author when possible, and logs the action to the Auto-Mod/general log channel. Server staff are exempt.

Anti-phishing uses local URL parsing only; it never visits the flagged URL. It is an extra safeguard, not a guarantee that every scam is detected, so keep Discord's native safety controls on.
