const solutionSections = Array.from(document.querySelectorAll("[data-solution-diagram]"));

solutionSections.forEach(section => {
  section.classList.add("solution-section--ready");
});

const diagramPathSpecs = [
  {
    inputLabel: "Raw Diff",
    inputPath: "solution-path-raw-diff",
    outputLabel: "Review Suggestions",
    outputPath: "solution-path-review-suggestions",
    routePath: "solution-route-raw-review",
    inputTarget: { x: 536, y: 238 },
    outputStart: { x: 1104, y: 238 },
    middle: "C 564 244 596 252 628 260 C 648 260 660 260 680 260 C 758 260 842 260 920 260 C 948 260 978 260 1010 260 C 1042 252 1074 244 1104 238"
  },
  {
    inputLabel: "Pull Request",
    inputPath: "solution-path-pull-request",
    outputLabel: "Soft Check",
    outputPath: "solution-path-soft-check",
    routePath: "solution-route-pr-soft-check",
    inputTarget: { x: 536, y: 254 },
    outputStart: { x: 1104, y: 254 },
    middle: "C 564 256 596 258 628 260 C 648 260 660 260 680 260 C 758 260 842 260 920 260 C 948 260 978 260 1010 260 C 1042 258 1074 256 1104 254"
  },
  {
    inputLabel: "Repository Snapshot",
    inputPath: "solution-path-repository-snapshot",
    outputLabel: "Evidence",
    outputPath: "solution-path-evidence",
    routePath: "solution-route-snapshot-evidence",
    inputTarget: { x: 536, y: 266 },
    outputStart: { x: 1104, y: 266 },
    middle: "C 564 264 596 262 628 260 C 648 260 660 260 680 260 C 758 260 842 260 920 260 C 948 260 978 260 1010 260 C 1042 262 1074 264 1104 266"
  },
  {
    inputLabel: "Changed Files",
    inputPath: "solution-path-changed-files",
    outputLabel: "Risk Notes",
    outputPath: "solution-path-risk-notes",
    routePath: "solution-route-files-risk",
    inputTarget: { x: 536, y: 282 },
    outputStart: { x: 1104, y: 282 },
    middle: "C 564 276 596 268 628 260 C 648 260 660 260 680 260 C 758 260 842 260 920 260 C 948 260 978 260 1010 260 C 1042 268 1074 276 1104 282"
  }
];

function formatPoint(value) {
  return Number(value.toFixed(1));
}

function toSvgPoint(svg, x, y) {
  const svgRect = svg.getBoundingClientRect();
  const viewBox = svg.viewBox.baseVal;

  return {
    x: formatPoint(viewBox.x + ((x - svgRect.left) / svgRect.width) * viewBox.width),
    y: formatPoint(viewBox.y + ((y - svgRect.top) / svgRect.height) * viewBox.height)
  };
}

function socketPoint(section, svg, label) {
  const chip = Array.from(section.querySelectorAll(".flow-chip")).find(item => item.textContent.includes(label));
  const socket = chip && chip.querySelector(".flow-chip__socket");

  if (!socket) return null;

  const rect = socket.getBoundingClientRect();
  return toSvgPoint(svg, rect.left + rect.width / 2, rect.top + rect.height / 2);
}

function leftConnectorPath(start, end) {
  const terminalX = formatPoint(Math.min(start.x + 24, end.x - 80));
  return `M ${start.x} ${start.y} H ${terminalX} C ${formatPoint(terminalX + 42)} ${start.y} ${formatPoint(end.x - 86)} ${end.y} ${end.x} ${end.y}`;
}

function rightConnectorTail(start, end) {
  const terminalX = formatPoint(Math.max(end.x - 24, start.x + 80));
  return `C ${formatPoint(start.x + 86)} ${start.y} ${formatPoint(terminalX - 42)} ${end.y} ${terminalX} ${end.y} H ${end.x}`;
}

function syncDiagramPaths(section) {
  const svg = section.querySelector(".diagram-connector");
  if (!svg) return;

  const svgRect = svg.getBoundingClientRect();
  if (!svgRect.width || !svgRect.height) return;

  diagramPathSpecs.forEach(spec => {
    const inputSocket = socketPoint(section, svg, spec.inputLabel);
    const outputSocket = socketPoint(section, svg, spec.outputLabel);

    if (!inputSocket || !outputSocket) return;

    const inputPath = leftConnectorPath(inputSocket, spec.inputTarget);
    const outputTail = rightConnectorTail(spec.outputStart, outputSocket);
    const outputPath = `M ${spec.outputStart.x} ${spec.outputStart.y} ${outputTail}`;

    svg.querySelector(`#${spec.inputPath}`)?.setAttribute("d", inputPath);
    svg.querySelector(`#${spec.outputPath}`)?.setAttribute("d", outputPath);
    svg.querySelector(`#${spec.routePath}`)?.setAttribute("d", `${inputPath} ${spec.middle} ${outputTail}`);
  });
}

function syncAllDiagramPaths() {
  solutionSections.forEach(syncDiagramPaths);
}

syncAllDiagramPaths();
window.addEventListener("resize", () => {
  window.requestAnimationFrame(syncAllDiagramPaths);
});

function startDiagramPackets(section) {
  const packets = Array.from(section.querySelectorAll(".diagram-data-packet"));

  packets.forEach(packet => {
    const delay = Number(packet.dataset.packetDelay || 0);

    window.setTimeout(() => {
      packet.querySelectorAll("animate, animateMotion").forEach(animation => {
        if (typeof animation.beginElement === "function") {
          animation.beginElement();
        }
      });
    }, delay);
  });
}

if ("IntersectionObserver" in window) {
  const solutionObserver = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        syncDiagramPaths(entry.target);
        entry.target.classList.add("is-visible");
        startDiagramPackets(entry.target);
        window.setTimeout(() => {
          entry.target.classList.add("solution-section--settled");
          syncDiagramPaths(entry.target);
        }, 2600);
        solutionObserver.unobserve(entry.target);
      });
    },
    { threshold: 0.28 }
  );

  solutionSections.forEach(section => solutionObserver.observe(section));
} else {
  solutionSections.forEach(section => {
    section.classList.add("is-visible", "solution-section--settled");
    syncDiagramPaths(section);
    startDiagramPackets(section);
  });
}
