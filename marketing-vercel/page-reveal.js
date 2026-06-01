const revealItems = Array.from(document.querySelectorAll(".reveal"));
const reduceMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

const initHeroTypingTitle = () => {
  const title = document.querySelector("[data-hero-typing-title]");
  if (!title) return;

  const lineNodes = Array.from(title.querySelectorAll("[data-hero-title-line]"));
  const textNodes = lineNodes.map(line => line.querySelector(".hero-title__text"));
  const lines = lineNodes.map(line => line.dataset.heroTitleLine ?? "");

  if (!lineNodes.length || textNodes.some(node => !node)) return;

  const complete = () => {
    lines.forEach((line, index) => {
      textNodes[index].textContent = line;
    });
    title.classList.remove("is-typing");
    title.classList.add("is-complete", "is-glowing");
  };

  if (reduceMotionQuery.matches) {
    title.classList.add("is-reduced-motion");
    complete();
    return;
  }

  const cursor = document.createElement("span");
  cursor.className = "hero-title__cursor";
  cursor.setAttribute("aria-hidden", "true");
  cursor.textContent = "|";

  const characters = lines.flatMap((line, lineIndex) =>
    Array.from(line).map((character, characterIndex) => ({
      character,
      lineIndex,
      characterIndex
    }))
  );

  const initialDelay = 560;
  const minSpeed = 35;
  const maxSpeed = 85;
  const rhythm = [46, 58, 42, 64, 50, 72, 38, 56, 68, 44];
  let characterIndex = 0;
  let timeoutId;

  title.classList.add("is-typing");
  textNodes[0].append(cursor);

  const typeNext = () => {
    if (characterIndex >= characters.length) {
      window.setTimeout(() => {
        title.classList.remove("is-typing");
        title.classList.add("is-complete");
        window.setTimeout(() => {
          cursor.remove();
          title.classList.add("is-glowing");
        }, 360);
      }, 140);
      return;
    }

    const current = characters[characterIndex];
    const typedText = lines[current.lineIndex].slice(0, current.characterIndex + 1);
    textNodes[current.lineIndex].textContent = typedText;
    textNodes[current.lineIndex].append(cursor);
    characterIndex += 1;

    const baseDelay = rhythm[characterIndex % rhythm.length];
    const spacePause = current.character === " " ? 26 : 0;
    const linePause = current.characterIndex === lines[current.lineIndex].length - 1 ? 120 : 0;
    const nextDelay = Math.max(minSpeed, Math.min(maxSpeed, baseDelay + spacePause)) + linePause;

    timeoutId = window.setTimeout(typeNext, nextDelay);
  };

  timeoutId = window.setTimeout(typeNext, initialDelay);

  reduceMotionQuery.addEventListener?.("change", event => {
    if (!event.matches) return;
    window.clearTimeout(timeoutId);
    cursor.remove();
    title.classList.add("is-reduced-motion");
    complete();
  });
};

document.documentElement.classList.add("reveal-ready");
initHeroTypingTitle();

revealItems.forEach((item, index) => {
  item.style.setProperty("--reveal-delay", `${(index % 4) * 90}ms`);
});

if ("IntersectionObserver" in window) {
  const revealObserver = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        revealObserver.unobserve(entry.target);
      });
    },
    {
      rootMargin: "0px 0px -12% 0px",
      threshold: 0.14
    }
  );

  revealItems.forEach(item => revealObserver.observe(item));
} else {
  revealItems.forEach(item => item.classList.add("is-visible"));
}
