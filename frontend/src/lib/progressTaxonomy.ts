export const PROFESSION_OPTIONS = [
  { value: "product_design", label: "产品/需求设计" },
  { value: "software_development", label: "软件开发" },
  { value: "data_integration", label: "数据与接口" },
  { value: "testing_uat", label: "测试/UAT" },
  { value: "implementation_go_live", label: "实施上线" },
];

export const BOARD_OPTIONS = [
  "原料", "铁区", "炼钢", "热轧", "冷轧", "仓储", "物流", "质量", "能源", "设备",
  "安全", "环保", "生产实绩", "计划", "成本",
].map((value) => ({ value, label: value }));
