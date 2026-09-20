/**
 * 高德 typecode → 中文大类映射（内部前端交接清单 20.4）
 *
 * 规则：
 * - 取值可能形如 "061000|110000"（竖线多类型）、"风景名胜;寺庙道观"（分号文本）、"140100"（纯数字）
 * - 统一取**最后一段**（更具体），再按**前两位大类**查表
 * - 大类粗映射：只覆盖对用户有意义的类别；映射不到返回 null → 调用方整行隐藏
 * - 🔴 不做高德全量 typecode（上千个，追了就是维护坑）
 */
const TYPECODE_LABELS: Record<string, string> = {
  '05': '餐饮',
  '06': '购物',
  '07': '生活服务',
  '08': '体育休闲',
  '09': '医疗保健',
  '10': '住宿',
  '11': '风景名胜',
  '13': '政府机构',
  '14': '科教文化',
  '15': '交通设施',
  '19': '旅游服务',
  '21': '旅游出行',
  '24': '公共设施',
  // 01-04 汽车系 / 12·18 商务住宅 / 16 金融保险 / 17 房地产 / 20 汽车租赁 /
  // 22 道路附属 / 23 地名地址 / 25 事件活动 / 26 门址 —— 对用户无展示价值 → 不在表内 → null → 隐藏
}

export function typecodeLabel(code: string | null): string | null {
  if (!code) return null
  const parts = code
    .split(/[;；|]/)
    .map((x) => x.trim())
    .filter(Boolean)
  const last = parts.length ? parts[parts.length - 1] : code
  const cls = last.slice(0, 2)
  return TYPECODE_LABELS[cls] ?? null
}
