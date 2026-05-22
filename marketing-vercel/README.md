# MergeWarden Vercel Marketing Site

这是一个独立静态宣传站，入口是 `index.html`。它不依赖 npm、不修改 Python runtime、CLI、FastAPI 或 GitHub Actions 代码。

## 本地预览

```powershell
python -m http.server 4173 -d E:\PycharmProjects\Debug\marketing-vercel
```

打开 `http://localhost:4173`。

## 部署

在 Vercel 中把项目根目录设置为 `marketing-vercel`。无需 build command，输出目录保持默认静态根目录。

## 产品边界

页面只宣传 MergeWarden 作为 advisory AI PR gatekeeper：它提供 soft check、结构化证据与 changed-line comments，不声称替代 CI、自动 approve 或接管 branch protection。
