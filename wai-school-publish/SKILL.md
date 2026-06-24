---
name: wai-school-publish
description: Publish a student's static browser project to wai.school and return a real wai.school/project/... link. Use when the user asks to publish, upload, deploy, share, get a link, or put an HTML/CSS/JS project on WAI School.
---

# WAI School Publish

Use this skill when the student wants to publish, update, upload, deploy, share, or get a public link for an HTML/CSS/JS project.

The goal is simple: bundle the current project, upload it to WAI School, and return the real link. If the project was already published from this folder, publish again to update the same link. Do not invent links.

## What To Do

1. Identify the project folder.
   - If the user attached or created a single `.html` file, use that file.
   - If the current workspace has `index.html`, use the current workspace.
   - If the project exists only as code in the chat, first save it as one local `.html` file, then publish that file.
   - If there are several folders, ask one short question: which folder should be published?
   - Local CSS, JS, images, audio, and fonts are allowed; the publisher will inline supported local assets.

2. Run a local validation first:

```bash
python3 scripts/publish_project.py --dry-run --dir .
```

If the project is a file or subfolder, pass that path:

```bash
python3 scripts/publish_project.py --dry-run --dir ./my-project
```

3. If validation succeeds, publish:

```bash
python3 scripts/publish_project.py --dir .
```

4. Read the script output.
   - If it returns `ok: true`, tell the student the exact `url`.
   - If it returns `updated: true`, say that the same link was updated.
   - If it returns an error, explain the exact problem and the smallest next fix.
   - Never claim the project was published unless the script returned a URL.

## Output Style

Use Russian by default.

Keep the final answer short:

```text
Готово, проект опубликован:
https://wai.school/project/...

Открой ссылку и проверь, что всё выглядит как нужно.
```

When updating an existing link:

```text
Готово, я обновил ту же ссылку:
https://wai.school/project/...

Открой и проверь новую версию.
```

If the server says it redacted personal data, mention it briefly:

```text
Сервер убрал личные данные перед публикацией. Проверь страницу по ссылке.
```

If Claude's code environment cannot reach `wai.school`, say this exactly and stop:

```text
Не получилось опубликовать автоматически: Claude сейчас не может подключиться к wai.school.
Позови ментора: нужно разрешить доступ к wai.school или опубликовать через страницу WAI School.
```

## Rules

- Do not upload `.env`, API keys, tokens, passwords, private keys, or backend source code.
- Publish only static student projects: HTML, CSS, JS, images, audio, fonts.
- If there is no HTML file, create one from the project code or ask one short question.
- Do not use another hosting service.
- Do not make up a `wai.school` URL.
- Do not keep retrying with random changes. Surface the server error.
- Keep `.wai-school-project.json` private and do not explain it to the student unless a mentor asks.
