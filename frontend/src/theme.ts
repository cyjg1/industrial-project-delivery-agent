import type { ThemeConfig } from "antd";

export const brand = {
  name: "交付助理",
  fullName: "交付助理 · 项目交付 AI",
  version: "0.5.0",
};

export const fontFamily = "PingFang SC, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";

export const tokens = {
  color: {
    bg: "#f5f6f8",
    surface: "#ffffff",
    surfaceSunken: "#fafbfc",
    border: "#e3e5e9",
    borderStrong: "#d0d3d9",
    text: "#1a1d21",
    textSecondary: "#5a6069",
    textTertiary: "#6b7178",
    primary: "#2563eb",
    primaryHover: "#1d4ed8",
    primarySoft: "#eef4ff",
    success: "#0f9d58",
    successSoft: "#e8f6ee",
    warning: "#b45309",
    warningSoft: "#fef4e6",
    danger: "#dc2626",
    dangerSoft: "#fdecec",
    info: "#2563eb",
    infoSoft: "#eef4ff",
  },
  radius: {
    sm: 6,
    md: 8,
    lg: 8,
  },
  shadow: {
    sm: "0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.04)",
    md: "0 4px 12px rgba(16,24,40,.08)",
  },
  font: {
    family: fontFamily,
    size: 14,
    sizeSm: 12,
    sizeLg: 16,
  },
} as const;

export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: tokens.color.primary,
    colorPrimaryHover: tokens.color.primaryHover,
    colorPrimaryBg: tokens.color.primarySoft,
    colorSuccess: tokens.color.success,
    colorSuccessBg: tokens.color.successSoft,
    colorWarning: tokens.color.warning,
    colorWarningBg: tokens.color.warningSoft,
    colorError: tokens.color.danger,
    colorErrorBg: tokens.color.dangerSoft,
    colorInfo: tokens.color.info,
    colorInfoBg: tokens.color.infoSoft,
    colorText: tokens.color.text,
    colorTextSecondary: tokens.color.textSecondary,
    colorTextTertiary: tokens.color.textTertiary,
    colorBorder: tokens.color.border,
    colorBorderSecondary: tokens.color.border,
    colorBgLayout: tokens.color.bg,
    colorBgContainer: tokens.color.surface,
    borderRadius: tokens.radius.sm,
    borderRadiusLG: tokens.radius.md,
    boxShadowTertiary: tokens.shadow.sm,
    fontFamily: tokens.font.family,
    fontSize: tokens.font.size,
    fontSizeSM: tokens.font.sizeSm,
    fontSizeLG: tokens.font.sizeLg,
    controlHeight: 34,
  },
  components: {
    Card: {
      headerBg: tokens.color.surface,
      boxShadowTertiary: tokens.shadow.sm,
    },
    Table: {
      headerBg: tokens.color.surfaceSunken,
      headerColor: tokens.color.textSecondary,
      rowHoverBg: tokens.color.primarySoft,
      borderColor: tokens.color.border,
    },
    Segmented: {
      itemSelectedBg: tokens.color.surface,
      trackBg: tokens.color.surfaceSunken,
    },
    Tooltip: {
      colorBgSpotlight: tokens.color.text,
    },
    Drawer: {
      colorBgElevated: tokens.color.surface,
    },
    Tag: {
      defaultBg: tokens.color.surfaceSunken,
      defaultColor: tokens.color.textSecondary,
    },
  },
};
