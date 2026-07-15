---
name: wai-school-publish
description: Publish a student's static browser project to wai.school and return a real wai.school/project/... link. Use when the user asks to publish, upload, deploy, share, get a link, or put an HTML/CSS/JS project on WAI School.
---

# WAI School Publish

Use this skill when the student wants to publish, update, upload, deploy, share, or get a public link for an HTML/CSS/JS project.

The installed skill is invoked as `/wai-school-publish`. Its bundled publisher must always be addressed through `${CLAUDE_SKILL_DIR}` so publishing works from any student project folder.

The goal is simple: bundle the current project, upload it to WAI School, and return the real link. If the project was already published from this folder, publish again to create the next version on the same link. Do not invent links.

## What To Do

1. Identify the project folder.
   - If the user attached or created a single `.html` file, use that file.
   - If the current workspace has `index.html`, use the current workspace.
   - If `package.json` defines a `build` script, run that existing script with the project's current lockfile package manager before validation (`pnpm build`, `npm run build`, `yarn build`, or `bun run build`). If the build fails, surface the exact failure; do not guess another command or publish stale source files.
   - The publisher automatically selects exactly one ready `dist/`, `build/`, `out/`, or `public/` root and uploads only files inside it. If it reports several ready builds, ask which build folder to publish and pass that folder explicitly.
   - The static build must use relative local asset paths such as `./assets/app.js`, because the project lives below `/project/<slug>/`. If dry-run reports `Use relative paths`, set the framework's public/base path to a relative value, rebuild, and rerun dry-run; do not patch hashed build files as a hidden workaround.
   - If the project exists only as code in the chat, first save it as one local `.html` file, then publish that file.
   - If there are several folders, ask one short question: which folder should be published?
   - Local CSS, JS, images, audio, video, JSON levels, fonts, GLB/GLTF models, and WASM are allowed; the publisher uploads supported local files with the project.
   - Keep the project compact: up to 400 files, 10 MB per file, 50 MB total local files.

2. Run a local validation first:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/publish_project.py --dry-run --dir .
```

If the project is a file or subfolder, pass that path:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/publish_project.py --dry-run --dir ./my-project
```

3. If validation succeeds, publish:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/publish_project.py --dir .
```

If the student is continuing an existing WAI School project from a new chat, pass the existing public link:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/publish_project.py --restore --project https://wai.school/project/... --publish-token <token> --dir ./wai-school-project
python3 ${CLAUDE_SKILL_DIR}/scripts/publish_project.py --dry-run --project https://wai.school/project/... --publish-token <token> --dir ./wai-school-project
python3 ${CLAUDE_SKILL_DIR}/scripts/publish_project.py --project https://wai.school/project/... --publish-token <token> --dir ./wai-school-project
```

The restore stores the current revision locally. Every later publish sends that revision, so two Claude chats cannot silently overwrite each other.

The publisher treats its local project state as an update capability: it writes it atomically with owner-only permissions and stops on corrupt or incomplete state instead of creating a duplicate project. Separate non-index HTML files in one folder receive separate state files; an ambiguous legacy folder must be split or checked by a mentor before publishing.

If the prompt includes a scoped `publish-token`, pass it as a CLI argument. Never save it in project files or repeat it to the student:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/publish_project.py --project https://wai.school/project/... --publish-token <token> --dir .
```

4. Read the script output.
   - If it returns `ok: true`, tell the student the exact `url`.
   - If it returns `updated: true`, say that the same link was updated.
   - If it says `Missing local project file`, create or fix the referenced local file/path, rerun `--dry-run`, then publish.
   - If it returns a validation error, explain it in plain words and suggest one small fix.
   - If it returns a server, network, or token error, tell the student: "Не получилось опубликовать автоматически. Позови ментора." Keep the exact error for the mentor.
   - If it reports a revision conflict, do not overwrite immediately. The current publisher restores the live project into a separate `*-live-vN` folder automatically. Compare that folder with the student's current files, merge the intended change into the restored folder, run dry-run, and publish from that restored folder. The current public version and the student's local work must both be preserved.
   - If it reports a corrupt, incomplete, or ambiguous safe publish state, stop. Do not delete the state or publish as a new project. Ask a mentor to repair the state or identify the correct live project first.
   - A legacy anonymous project state without a revision is handled the same way: its saved edit capability restores the current source into `*-live-current` and stops before publishing. Never bypass that stop with a guessed revision.
   - Never claim the project was published unless the script returned a URL.

The server keeps the latest 20 quick-publish versions. A child can inspect them, download the exact multi-file sources, or restore an older version from “Мои проекты”. Restoring never erases history: it creates a new current revision on the same public link.

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
- Publish only static student projects: HTML, CSS, JS, images, audio, video, fonts, JSON, GLB/GLTF, and WASM.
- Keep everything local inside the project folder: no CDN, external API calls, WebSocket/EventSource, service workers, cookies, localStorage, sessionStorage, IndexedDB, or Cache API.
- Use a clean project folder so unrelated local files are not uploaded by mistake.
- If there is no HTML file, create one from the project code or ask one short question.
- Do not use another hosting service.
- Do not make up a `wai.school` URL.
- Do not keep retrying with random changes. Surface the server error.
- Do not bypass a revision conflict with a guessed `--base-revision`. Use it only after checking that exact live revision and intentionally merging the files.
- Keep `.wai-school-project.json` private and do not explain it to the student unless a mentor asks.
