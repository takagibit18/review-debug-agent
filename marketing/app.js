const root = document.documentElement;
const reduceMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

const translations = {
  zh: {
    meta: {
      title: "MergeWarden - 给 PR 合并前补一层证据软检查",
      description:
        "MergeWarden 是面向 GitHub PR 的 advisory AI 审查层，把 diff、changed lines、测试输出和运行摘要转成可复盘的软检查信号。"
    },
    nav: {
      skip: "跳到主要内容",
      aria: "主导航",
      homeLabel: "MergeWarden 首页",
      sections: "页面区块",
      mobileSections: "移动端页面导航",
      menuOpen: "打开页面导航",
      menuClose: "关闭页面导航",
      backToTop: "回到顶部",
      language: "语言切换",
      githubLabel: "在新标签页打开 MergeWarden GitHub 仓库",
      gap: "风险证据",
      workflow: "工作流",
      github: "GitHub 场景",
      proof: "证明",
      faq: "FAQ"
    },
    hero: {
      lede: "让 PR 合并前多一层可复盘的证据软检查。",
      body:
        "它把 diff、changed lines、测试输出和运行摘要整理成 GitHub reviewer 能读懂的 neutral advisory signal。团队获得更清楚的风险证据，但合并判断仍留给 CI、branch protection 和 human reviewer。",
      primary: "理解产品价值",
      secondary: "查看 GitHub 场景",
      boundaryLabel: "产品边界",
      note1: "advisory soft check",
      note2: "changed-line comments",
      note3: "CI 保留硬门禁",
      sceneLabel: "GitHub PR advisory 产品场景",
      sceneAlt: "MergeWarden 在 GitHub PR 中发布 neutral check、changed-line comments 和 run summary evidence 的产品场景",
      demoLabel: "产品场景切换",
      demoRisk: "风险证据",
      demoSummary: "运行摘要",
      demoBoundary: "权限边界"
    },
    demo: {
      riskTitle: "找到风险，但不抢合并权限。",
      riskCopy: "可评论的问题必须落回 changed line；无法精准定位的发现只进入 check summary。",
      summaryTitle: "把运行过程留成 artifact。",
      summaryCopy: "run summary 记录模型、工具、token、stop reason 和 publish 状态，便于复盘。",
      boundaryTitle: "保持 neutral，不替代 CI。",
      boundaryCopy: "MergeWarden 输出证据和建议；CI、branch protection 与 reviewer 仍决定能否合并。"
    },
    gap: {
      title: "PR 速度变快，风险证据却经常掉队。",
      body:
        "自动测试证明已知契约，reviewer 还要判断变更假设、危险默认值、缺失上下文和失败日志。MergeWarden 的价值不是制造更多评论，而是把“可能有问题”整理成“这里需要验证”的证据。",
      listLabel: "MergeWarden 证据模型",
      card1Title: "只从变更开始",
      card1Copy: "PR diff 是审查目标，仓库快照只是上下文。",
      card2Title: "只在可定位处评论",
      card2Copy: "Inline feedback 必须指回 changed line 或 changed hunk。",
      card3Title: "每条建议带证据",
      card3Copy: "Severity、location、evidence、suggestion 和 confidence 一起输出。"
    },
    workflow: {
      title: "从 PR diff 到可发布的 review 证据。",
      body:
        "页面中部不讲抽象 AI 能力，而讲真实接入路径：diff 生成 changed-line map，review 输出和 run summary 共同决定 check summary 与 inline comments。",
      alt: "PR diff 到 changed_lines.json、review_response、run_summary、neutral check 与 comments 的工作流"
    },
    github: {
      title: "第一产品入口，就在 GitHub PR 里。",
      body:
        "首版不需要新 dashboard。MergeWarden 先作为 GitHub Actions 中的 advisory publisher：产出 neutral check、必要的 changed-line comments 和可下载 artifacts。",
      calloutTitle: "边界必须清楚",
      calloutCopy: "它提供风险证据，不替代 CI、不自动 approve、不接管 branch protection。",
      copy: "复制",
      copied: "已复制"
    },
    proof: {
      title: "价值来自证据链，而不是更大的权限。",
      body:
        "MergeWarden 把运行过程、评论生命周期和权限边界都变成可检查 artifacts，让团队能复盘为什么某条 advisory 出现、更新或被标记为 stale。",
      alt: "changed-line eligibility、run summary、comment fingerprint 和 neutral check 四类 proof artifacts",
      card1Title: "Changed-line eligible",
      card1Copy: "只有能落回变更行的问题才发 inline comment。",
      card2Title: "Run summary artifact",
      card2Copy: "模型、工具、token、stop reason 和 publish 状态都可复盘。",
      card3Title: "Neutral by contract",
      card3Copy: "check run 传递风险信号，但不伪装成硬性合并裁决。"
    },
    faq: {
      title: "它靠近合并流程，所以边界要写在页面上。",
      body: "宣传页的目标是帮助人理解 MergeWarden 的产品价值，而不是暗示它已经是一个全权限 merge gate。",
      q1: "它会替代 CI 吗？",
      a1: "不会。CI 和 branch protection 仍是硬门禁，MergeWarden 只发布 neutral advisory signal。",
      q2: "它会评论未改动文件吗？",
      a2: "未改动文件可以作为证据上下文，但 inline comments 默认只落在 changed line 或 changed hunk。",
      q3: "为什么不先做 dashboard？",
      a3: "第一产品回路在 GitHub PR 内更短：reviewer 已经在那里处理风险、讨论和合并判断。",
      q4: "这个页面如何部署？",
      a4: "`marketing-vercel/` 可作为 Vercel 静态根目录；`marketing/` 是同步静态镜像。"
    },
    cta: {
      title: "把更清楚的 review 证据放回每个 PR。",
      body: "MergeWarden 的定位不是“自动决定能不能合并”，而是让 reviewer 更快看见风险、证据和下一步验证。",
      github: "查看 GitHub",
      workflow: "回看工作流"
    },
    footer: {
      boundary: "仅 advisory。CI 保留合并硬门禁。"
    }
  },
  en: {
    meta: {
      title: "MergeWarden - Evidence-first advisory checks for pull requests",
      description:
        "MergeWarden is an advisory AI review layer for GitHub pull requests, turning diffs, changed lines, test output, and run summaries into reproducible soft-check signals."
    },
    nav: {
      skip: "Skip to main content",
      aria: "Main navigation",
      homeLabel: "MergeWarden home",
      sections: "Page sections",
      mobileSections: "Mobile page navigation",
      menuOpen: "Open page navigation",
      menuClose: "Close page navigation",
      backToTop: "Back to top",
      language: "Language switcher",
      githubLabel: "Open the MergeWarden GitHub repository in a new tab",
      gap: "Risk evidence",
      workflow: "Workflow",
      github: "GitHub scene",
      proof: "Proof",
      faq: "FAQ"
    },
    hero: {
      lede: "Evidence-first soft checks before a PR gets merged.",
      body:
        "MergeWarden turns diffs, changed lines, test output, and run summaries into neutral advisory signals that GitHub reviewers can understand. Teams get clearer risk evidence while CI, branch protection, and human reviewers keep merge authority.",
      primary: "Understand the value",
      secondary: "See GitHub scene",
      boundaryLabel: "Product boundary",
      note1: "advisory soft check",
      note2: "changed-line comments",
      note3: "CI keeps the hard gate",
      sceneLabel: "GitHub PR advisory product scene",
      sceneAlt: "MergeWarden product scene showing a neutral check, changed-line comments, and run summary evidence inside a GitHub pull request",
      demoLabel: "Product scene switcher",
      demoRisk: "Risk evidence",
      demoSummary: "Run summary",
      demoBoundary: "Boundary"
    },
    demo: {
      riskTitle: "Find risk without taking merge authority.",
      riskCopy: "Commentable findings must land on changed lines; uncertain locations stay in the check summary.",
      summaryTitle: "Keep the run explainable as an artifact.",
      summaryCopy: "Run summaries record model, tools, tokens, stop reasons, and publish status for later review.",
      boundaryTitle: "Stay neutral. Do not replace CI.",
      boundaryCopy: "MergeWarden emits evidence and advice; CI, branch protection, and reviewers decide mergeability."
    },
    gap: {
      title: "PRs move faster than the evidence around them.",
      body:
        "Automated tests prove known contracts. Reviewers still need to reason about assumptions, dangerous defaults, missing context, and failing logs. MergeWarden turns vague suspicion into evidence that says where to verify next.",
      listLabel: "MergeWarden evidence model",
      card1Title: "Start from the change",
      card1Copy: "The PR diff is the target. The repository snapshot is only context.",
      card2Title: "Comment only where grounded",
      card2Copy: "Inline feedback must point back to a changed line or changed hunk.",
      card3Title: "Attach evidence to advice",
      card3Copy: "Severity, location, evidence, suggestion, and confidence ship together."
    },
    workflow: {
      title: "From PR diff to publishable review evidence.",
      body:
        "The page explains the real product loop, not abstract AI capability: a diff becomes a changed-line map, then review output and a run summary decide the check summary and inline comments.",
      alt: "Workflow from PR diff to changed_lines.json, review_response, run_summary, neutral check, and advisory comments"
    },
    github: {
      title: "The first product surface is inside the GitHub PR.",
      body:
        "The first version does not need a new dashboard. MergeWarden works as an advisory publisher in GitHub Actions: a neutral check, necessary changed-line comments, and downloadable artifacts.",
      calloutTitle: "The boundary is explicit",
      calloutCopy: "It provides risk evidence. It does not replace CI, auto-approve, or take over branch protection.",
      copy: "Copy",
      copied: "Copied"
    },
    proof: {
      title: "The value is evidence, not more authority.",
      body:
        "MergeWarden makes runtime behavior, comment lifecycle, and permission boundaries inspectable as artifacts, so teams can review why an advisory appeared, changed, or became stale.",
      alt: "Four proof artifacts: changed-line eligibility, run summary, comment fingerprint, and neutral check",
      card1Title: "Changed-line eligible",
      card1Copy: "Only findings that map to changed lines become inline comments.",
      card2Title: "Run summary artifact",
      card2Copy: "Models, tools, tokens, stop reasons, and publish status stay reviewable.",
      card3Title: "Neutral by contract",
      card3Copy: "The check run carries risk signal without pretending to be a hard merge decision."
    },
    faq: {
      title: "It sits near merge flow, so the boundary has to be visible.",
      body: "The page should help people understand MergeWarden's value, not imply that it is a full-permission merge gate.",
      q1: "Does it replace CI?",
      a1: "No. CI and branch protection remain the hard gate. MergeWarden publishes neutral advisory signal.",
      q2: "Will it comment on unchanged files?",
      a2: "Unchanged files can support the evidence, but inline comments default to changed lines or changed hunks.",
      q3: "Why not start with a dashboard?",
      a3: "The shortest first product loop is inside the PR, where reviewers already handle risk, discussion, and merge judgment.",
      q4: "How is this page deployed?",
      a4: "`marketing-vercel/` can be served as the Vercel static root; `marketing/` is the synced static mirror."
    },
    cta: {
      title: "Put clearer review evidence back into every PR.",
      body: "MergeWarden is not an automatic merge decision. It helps reviewers see risk, evidence, and the next verification step faster.",
      github: "View GitHub",
      workflow: "Review workflow"
    },
    footer: {
      boundary: "Advisory only. CI keeps the merge gate."
    }
  }
};

const getTranslation = (language, key) => {
  return key.split(".").reduce((value, part) => value?.[part], translations[language]) ?? "";
};

const setLanguage = language => {
  const safeLanguage = language === "en" ? "en" : "zh";
  root.lang = safeLanguage === "zh" ? "zh-CN" : "en";
  document.title = translations[safeLanguage].meta.title;

  document.querySelectorAll("[data-i18n]").forEach(node => {
    node.textContent = getTranslation(safeLanguage, node.dataset.i18n);
  });

  document.querySelectorAll("[data-i18n-attr]").forEach(node => {
    node.dataset.i18nAttr.split(",").forEach(binding => {
      const [attribute, key] = binding.split(":");
      node.setAttribute(attribute, getTranslation(safeLanguage, key));
    });
  });

  document.querySelectorAll("[data-lang-option]").forEach(button => {
    const isActive = button.dataset.langOption === safeLanguage;
    button.setAttribute("aria-pressed", String(isActive));
  });

  const isMenuOpen = document.body.classList.contains("is-nav-open");
  mobileMenuToggle?.setAttribute("aria-label", getTranslation(safeLanguage, isMenuOpen ? "nav.menuClose" : "nav.menuOpen"));
  localStorage.setItem("mergewarden-language", safeLanguage);
};

const setScrollProgress = () => {
  const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
  const progress = maxScroll > 0 ? (window.scrollY / maxScroll) * 100 : 0;
  const clampedProgress = Math.min(100, Math.max(0, progress));
  root.style.setProperty("--scroll-progress", `${clampedProgress}%`);
  backToTopButton?.classList.toggle("is-visible", clampedProgress > 40);
};

const revealItems = Array.from(document.querySelectorAll(".reveal"));
revealItems.forEach((item, index) => {
  item.style.setProperty("--stagger", `${Math.min(index, 12) * 60}ms`);
});

if ("IntersectionObserver" in window) {
  const revealObserver = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          revealObserver.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.15 }
  );
  revealItems.forEach(item => revealObserver.observe(item));
} else {
  revealItems.forEach(item => item.classList.add("is-visible"));
}

const navGroups = Array.from(document.querySelectorAll("[data-section-nav][href^='#']")).reduce((groups, link) => {
  const id = link.getAttribute("href").slice(1);
  const links = groups.get(id) ?? [];
  links.push(link);
  groups.set(id, links);
  return groups;
}, new Map());

if ("IntersectionObserver" in window && navGroups.size) {
  const sectionObserver = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        navGroups.forEach(links => links.forEach(link => link.classList.remove("is-active")));
        navGroups.get(entry.target.id)?.forEach(link => link.classList.add("is-active"));
      });
    },
    { rootMargin: "-42% 0px -52% 0px", threshold: 0.01 }
  );
  document.querySelectorAll("main section[id]").forEach(section => sectionObserver.observe(section));
}

const mobileMenuToggle = document.querySelector(".mobile-menu-toggle");
const mobileMenuLinks = Array.from(document.querySelectorAll("[data-mobile-menu-link]"));
const mobileMenuCloseTargets = Array.from(document.querySelectorAll("[data-mobile-menu-close]"));
const backToTopButton = document.querySelector(".back-to-top");

const setMobileMenuOpen = isOpen => {
  document.body.classList.toggle("is-nav-open", isOpen);
  mobileMenuToggle?.setAttribute("aria-expanded", String(isOpen));
  const language = localStorage.getItem("mergewarden-language") === "en" ? "en" : "zh";
  mobileMenuToggle?.setAttribute("aria-label", getTranslation(language, isOpen ? "nav.menuClose" : "nav.menuOpen"));
};

mobileMenuToggle?.addEventListener("click", () => {
  setMobileMenuOpen(!document.body.classList.contains("is-nav-open"));
});

mobileMenuLinks.forEach(link => {
  link.addEventListener("click", () => setMobileMenuOpen(false));
});

mobileMenuCloseTargets.forEach(target => {
  target.addEventListener("click", () => setMobileMenuOpen(false));
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    setMobileMenuOpen(false);
  }
});

backToTopButton?.addEventListener("click", () => {
  window.scrollTo({ top: 0, behavior: reduceMotionQuery.matches ? "auto" : "smooth" });
});

const demoButtons = Array.from(document.querySelectorAll("[data-demo]"));
const demoTitle = document.querySelector("[data-demo-title]");
const demoCopy = document.querySelector("[data-demo-copy]");

const setDemo = key => {
  const language = localStorage.getItem("mergewarden-language") === "en" ? "en" : "zh";
  demoButtons.forEach(button => {
    const isActive = button.dataset.demo === key;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-selected", String(isActive));
  });
  demoTitle.textContent = getTranslation(language, `demo.${key}Title`);
  demoCopy.textContent = getTranslation(language, `demo.${key}Copy`);
};

demoButtons.forEach(button => {
  button.addEventListener("click", () => setDemo(button.dataset.demo));
});

document.querySelectorAll("[data-lang-option]").forEach(button => {
  button.addEventListener("click", () => {
    setLanguage(button.dataset.langOption);
    const activeDemo = demoButtons.find(item => item.classList.contains("is-active"))?.dataset.demo ?? "risk";
    setDemo(activeDemo);
  });
});

const copyButton = document.querySelector("[data-copy]");
if (copyButton) {
  copyButton.addEventListener("click", async () => {
    const target = document.querySelector(copyButton.dataset.copy);
    if (!target) return;
    const language = localStorage.getItem("mergewarden-language") === "en" ? "en" : "zh";
    const text = target.textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
    } catch (error) {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      // Legacy clipboard fallback for browsers without navigator.clipboard.
      document.execCommand("copy");
      textarea.remove();
    }
    copyButton.textContent = getTranslation(language, "github.copied");
    window.setTimeout(() => {
      copyButton.textContent = getTranslation(language, "github.copy");
    }, 1600);
  });
}

setScrollProgress();
window.addEventListener("scroll", setScrollProgress, { passive: true });
window.addEventListener("resize", setScrollProgress);

const storedLanguage = localStorage.getItem("mergewarden-language");
setLanguage(storedLanguage === "en" ? "en" : "zh");

if (reduceMotionQuery.matches) {
  revealItems.forEach(item => item.classList.add("is-visible"));
}
