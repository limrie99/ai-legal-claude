# Share The AI Legal Assistant Online

This guide shows how to put the browser app online so someone else can open a link, upload or paste a contract, and run the AI review.

## Best Option: Render

Render can host the Python backend and the frontend together. This is the safest simple option because the Anthropic API key stays on the server as a private environment variable. Do not put the API key in the frontend or in GitHub Pages.

[Deploy on Render](https://render.com/deploy?repo=https://github.com/limrie99/ai-legal-claude)

### What You Need

- A GitHub account with access to this repository.
- A Render account: <https://render.com>
- An Anthropic API key from <https://console.anthropic.com>

### Deploy Steps

1. Push this repository to GitHub.
2. Click **Deploy on Render** above, or go to Render and choose **New +** then **Blueprint**.
3. Connect the GitHub repository.
4. Render will detect `render.yaml`.
5. When Render asks for environment variables, add:

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` |

6. Click **Apply** or **Deploy**.
7. After deployment, Render gives you a URL like:

```text
https://ai-legal-assistant.onrender.com
```

Send that URL to the person who needs to use the tool.

## Cost And Safety Notes

- Real AI analysis uses your Anthropic API key, so usage may cost money.
- Anyone with the URL may be able to run analyses unless you add login or password protection.
- This tool is not legal advice and should not replace a licensed attorney.
- Do not ask users to upload highly sensitive contracts unless you understand the hosting, privacy, and API data handling implications.

## Optional: Add Basic Privacy Protection

Before sharing broadly, consider adding one of these:

- Render service authentication or a simple password gate.
- A private URL shared only with trusted people.
- Usage limits or logging so unexpected traffic does not create surprise API costs.

## Why Not GitHub Pages?

GitHub Pages can host static HTML, CSS, and JavaScript, but this app needs a backend to call Claude safely. Putting an API key in frontend JavaScript would expose it to anyone who opens the page.

Use GitHub Pages only for a demo that does not call the real AI backend.
