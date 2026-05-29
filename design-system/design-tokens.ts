export const tokens = {
  colors: {
    bgBase: "#030820",
    bgDeep: "#010414",
    bgInk: "#050A24",
    bgPanel: "rgba(14,22,58,0.72)",
    bgPanelDeep: "rgba(6,10,32,0.82)",
    textPrimary: "#F8FAFF",
    textSecondary: "rgba(255,255,255,0.62)",
    textMuted: "rgba(255,255,255,0.35)",
    textInverse: "#11162A",
    bluePrimary: "#5A9BFF",
    blueSoft: "rgba(90,155,255,0.62)",
    blueLine: "rgba(96,160,255,0.85)",
    blueGlow: "rgba(86,148,255,0.45)",
    borderStrong: "rgba(105,145,255,0.45)",
    borderWeak: "rgba(105,145,255,0.22)",
    borderBright: "rgba(110,165,255,0.8)",
    white: "#FFFFFF",
  },

  typography: {
    fontSans:
      "\"Suisse Int'l\", \"Neue Haas Grotesk\", Inter, Geist, sans-serif",
    fontMono:
      "\"IBM Plex Mono\", \"Geist Mono\", \"Roboto Mono\", monospace",
    hero: {
      fontFamily: "var(--font-sans)",
      fontSize: "clamp(72px, 7vw, 140px)",
      mobileFontSize: "clamp(48px, 13vw, 72px)",
      lineHeight: "1.05",
      fontWeight: 300,
      letterSpacing: "0",
      maxWidth: "1400px",
    },
    sectionTitle: {
      fontFamily: "var(--font-sans)",
      fontSize: "clamp(48px, 4.6vw, 78px)",
      lineHeight: "1.15",
      fontWeight: 300,
      letterSpacing: "0",
      maxWidth: "1280px",
    },
    footerCtaTitle: {
      fontFamily: "var(--font-sans)",
      fontSize: "clamp(52px, 4vw, 64px)",
      lineHeight: "1.1",
      fontWeight: 300,
      letterSpacing: "0",
    },
    cardTitle: {
      fontFamily: "var(--font-sans)",
      fontSize: "clamp(24px, 1.6vw, 32px)",
      lineHeight: "1.2",
      fontWeight: 400,
      letterSpacing: "0",
    },
    cardBody: {
      fontFamily: "var(--font-sans)",
      fontSize: "clamp(17px, 1.05vw, 21px)",
      lineHeight: "1.42",
      fontWeight: 600,
      letterSpacing: "0",
    },
    nav: {
      fontFamily: "var(--font-mono)",
      fontSize: "16px",
      lineHeight: "1",
      fontWeight: 500,
      letterSpacing: "0.02em",
    },
    label: {
      fontFamily: "var(--font-mono)",
      fontSize: "16px",
      lineHeight: "1",
      fontWeight: 400,
      letterSpacing: "0.02em",
    },
    button: {
      fontFamily: "var(--font-mono)",
      fontSize: "16px",
      lineHeight: "1",
      fontWeight: 500,
      letterSpacing: "0.02em",
    },
    giantLogo: {
      fontFamily: "var(--font-sans)",
      fontSize: "clamp(260px, 16vw, 320px)",
      mobileFontSize: "clamp(72px, 22vw, 120px)",
      lineHeight: "0.8",
      fontWeight: 800,
      letterSpacing: "-0.06em",
      transform: "scaleX(1.08)",
    },
  },

  spacing: {
    px0: "0",
    px4: "4px",
    px6: "6px",
    px8: "8px",
    px12: "12px",
    px16: "16px",
    px20: "20px",
    px24: "24px",
    px32: "32px",
    px40: "40px",
    px44: "44px",
    px52: "52px",
    px64: "64px",
    px80: "80px",
    px96: "96px",
    px120: "120px",
    px140: "140px",
    px160: "160px",
    px220: "220px",
    sectionY: "clamp(140px, 16vh, 220px)",
    sectionGap: "clamp(120px, 14vh, 200px)",
    cardGap: "clamp(24px, 1.6vw, 32px)",
    labelToLine: "38px", // approximation
    lineToTitle: "52px", // approximation
    titleToCards: "clamp(90px, 9vh, 120px)",
  },

  layout: {
    pagePaddingDesktop: "52px",
    pagePaddingMobile: "20px",
    maxContent: "1680px",
    maxTitle: "1400px",
    maxSectionTitle: "1280px",
    headerTop: "24px",
    headerZIndex: 50,
    heroTitleTopDesktop: "36vh",
    heroTitleTopMobile: "30vh",
    heroTitleWidthDesktop: "70vw", // approximation
    sectionMinHeightHero: "100vh",
    sectionMinHeightMission: "90vh",
    sectionMinHeightProblem: "100vh",
    sectionMinHeightSolution: "110vh",
    sectionMinHeightFooter: "100vh",
    cardWidthDesktop: "min(920px, calc(50vw - 70px))", // approximation
    cardHeightDesktop: "545px", // approximation
    problemTrackPeek: "18vw", // approximation
  },

  radius: {
    button: "6px",
    pill: "4px",
    card: "16px",
    node: "14px",
    diagramPill: "28px",
  },

  border: {
    hairline: "1px solid rgba(105,145,255,0.22)",
    strong: "1px solid rgba(105,145,255,0.45)",
    bright: "1px solid rgba(110,165,255,0.8)",
    divider: "1px solid rgba(96,160,255,0.85)",
    node: "1.25px solid rgba(105,145,255,0.45)",
  },

  glow: {
    heroRadial:
      "radial-gradient(ellipse at center, rgba(220,235,255,0.78) 0%, rgba(83,145,235,0.72) 28%, rgba(3,8,32,0) 68%)",
    sectionRadial:
      "radial-gradient(ellipse at center, rgba(83,145,235,0.42) 0%, rgba(3,8,32,0) 62%)",
    cardInner:
      "inset 0 -80px 120px rgba(86,148,255,0.16), inset 0 1px 0 rgba(255,255,255,0.04)",
    cardHover: "0 0 36px rgba(75,130,255,0.22)",
    nodeGlow: "0 0 28px rgba(86,148,255,0.28)",
    footerRadial:
      "radial-gradient(ellipse at bottom, rgba(95,147,232,0.86) 0%, rgba(23,58,138,0.62) 36%, rgba(3,8,32,0) 74%)",
    vignette:
      "radial-gradient(ellipse at center, rgba(3,8,32,0) 42%, rgba(1,4,20,0.82) 100%)",
  },

  shadow: {
    none: "none",
    card: "none",
    button: "none",
    subtleBlue: "0 0 24px rgba(75,130,255,0.18)",
  },

  motion: {
    easeOutExpo: "cubic-bezier(0.16, 1, 0.3, 1)",
    durationFast: "240ms",
    durationLine: "500ms",
    durationNormal: "900ms",
    durationSlow: "1400ms",
    heroEnter: {
      opacity: "0 to 1",
      translateY: "30px to 0",
      duration: "900ms",
      easing: "cubic-bezier(0.16, 1, 0.3, 1)",
    },
    cardEnter: {
      opacity: "0 to 1",
      translateX: "80px to 0",
      stagger: "120ms",
      duration: "900ms",
    },
    hoverLift: {
      translateY: "-4px",
      duration: "240ms",
    },
  },

  breakpoints: {
    mobileMax: "767px",
    tabletMin: "768px",
    tabletMax: "1279px",
    desktopMin: "1280px",
  },
} as const;

export type DesignTokens = typeof tokens;
