const reduceMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
const root = document.documentElement;

const setScrollProgress = () => {
  const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
  const progress = maxScroll > 0 ? (window.scrollY / maxScroll) * 100 : 0;
  root.style.setProperty("--scroll-progress", `${Math.min(100, Math.max(0, progress))}%`);
};

setScrollProgress();
window.addEventListener("scroll", setScrollProgress, { passive: true });
window.addEventListener("resize", setScrollProgress);

const revealItems = Array.from(document.querySelectorAll(".reveal"));
revealItems.forEach((item, index) => {
  item.style.setProperty("--stagger", `${Math.min(index % 5, 4) * 70}ms`);
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
    { threshold: 0.16 }
  );

  revealItems.forEach(item => revealObserver.observe(item));
} else {
  revealItems.forEach(item => item.classList.add("is-visible"));
}

const steps = Array.from(document.querySelectorAll("[data-step-rail] .step"));
let activeStep = 0;
let stepTimer = null;

const setActiveStep = index => {
  if (!steps.length) return;
  activeStep = (index + steps.length) % steps.length;
  steps.forEach((step, stepIndex) => {
    const isActive = stepIndex === activeStep;
    step.classList.toggle("is-active", isActive);
    step.setAttribute("aria-pressed", String(isActive));
  });
};

const startStepTimer = () => {
  if (!steps.length || reduceMotionQuery.matches || stepTimer) return;
  stepTimer = window.setInterval(() => {
    setActiveStep(activeStep + 1);
  }, 2800);
};

const stopStepTimer = () => {
  if (!stepTimer) return;
  window.clearInterval(stepTimer);
  stepTimer = null;
};

steps.forEach((step, index) => {
  step.addEventListener("click", () => {
    setActiveStep(index);
    stopStepTimer();
    startStepTimer();
  });
});

startStepTimer();
const handleMotionPreference = event => {
  if (event.matches) {
    stopStepTimer();
  } else {
    startStepTimer();
  }
};

if (typeof reduceMotionQuery.addEventListener === "function") {
  reduceMotionQuery.addEventListener("change", handleMotionPreference);
} else if (typeof reduceMotionQuery.addListener === "function") {
  reduceMotionQuery.addListener(handleMotionPreference);
}

const navLinks = new Map(
  Array.from(document.querySelectorAll(".nav a[href^='#']")).map(link => [
    link.getAttribute("href").slice(1),
    link
  ])
);

if ("IntersectionObserver" in window && navLinks.size) {
  const sectionObserver = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        navLinks.forEach(link => link.classList.remove("is-active"));
        navLinks.get(entry.target.id)?.classList.add("is-active");
      });
    },
    { rootMargin: "-42% 0px -52% 0px", threshold: 0.01 }
  );

  document.querySelectorAll("main section[id]").forEach(section => {
    sectionObserver.observe(section);
  });
}

const parallaxLayers = Array.from(document.querySelectorAll(".parallax-layer"));
let parallaxFrame = 0;
let pointerX = 0;
let pointerY = 0;

const renderParallax = () => {
  parallaxFrame = 0;
  parallaxLayers.forEach(layer => {
    const depth = Number(layer.dataset.depth || 0.04);
    layer.style.setProperty("--px", `${pointerX * depth * 120}px`);
    layer.style.setProperty("--py", `${pointerY * depth * 120}px`);
  });
};

const updateParallax = event => {
  if (reduceMotionQuery.matches || !parallaxLayers.length) return;
  pointerX = event.clientX / window.innerWidth - 0.5;
  pointerY = event.clientY / window.innerHeight - 0.5;
  if (!parallaxFrame) {
    parallaxFrame = window.requestAnimationFrame(renderParallax);
  }
};

if (window.matchMedia("(pointer: fine)").matches) {
  window.addEventListener("pointermove", updateParallax, { passive: true });
}

const copyButton = document.querySelector("[data-copy]");
if (copyButton) {
  copyButton.addEventListener("click", async () => {
    const target = document.querySelector(copyButton.dataset.copy);
    if (!target) return;

    const text = target.textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
      copyButton.textContent = "已复制";
      copyButton.setAttribute("aria-label", "workflow 已复制");
    } catch (error) {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
      copyButton.textContent = "已复制";
      copyButton.setAttribute("aria-label", "workflow 已复制");
    }

    window.setTimeout(() => {
      copyButton.textContent = "复制";
      copyButton.setAttribute("aria-label", "复制 workflow");
    }, 1800);
  });
}
