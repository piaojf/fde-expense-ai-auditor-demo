/** Explain input failures without changing user-entered facts or approval rules. */
export function hotelDateError(start: string, end: string): string | null {
  const missing = [!start && '入住日期', !end && '退房日期'].filter(Boolean);
  if (missing.length) return `请填写${missing.join('、')}。`;
  function timestamp(value: string) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
    const time = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value ? time : NaN;
  }
  const from = timestamp(start), to = timestamp(end);
  if (!Number.isFinite(from)) return '入住日期无效，请填写有效的年月日。';
  if (!Number.isFinite(to)) return '退房日期无效，请填写有效的年月日。';
  const nights = (to - from) / 86400000;
  if (nights <= 0) return `入住日期为 ${start}，退房日期为 ${end}；退房日期必须晚于入住日期，至少住宿 1 晚。`;
  if (nights > 365) return `入住日期为 ${start}，退房日期为 ${end}，共 ${nights} 晚，超过最多 365 晚；请核对入住、退房年份和日期。`;
  return null;
}

const fieldLabels: Record<string, string> = {
  applicant_name: '申请人', department: '部门', position: '职位', category: '费用类型',
  city: '实际住宿城市／区县', amount: '报销金额', purpose: '出差事由／业务用途',
  incurred_on: '费用发生日期', trip_start: '入住日期', trip_end: '退房日期', nights: '住宿晚数',
  seller: '酒店名称（发票销售方）', notes: '备注', invoice_number: '发票号码',
  invoice_code: '发票代码', issue_date: '开票日期', seller_name: '发票销售方',
  buyer_name: '发票购买方', total_amount: '发票金额', tax_amount: '发票税额',
};

export function invoiceValidationMessage(details: Record<string, unknown>): string {
  if (!Array.isArray(details.issues)) return '表单校验未通过，请检查报销信息和票据字段。';
  const messages = details.issues.flatMap((issue: unknown) => {
    if (!issue || typeof issue !== 'object') return [];
    const { location, type, message } = issue as Record<string, unknown>;
    const field = Array.isArray(location) ? String(location[location.length - 1]) : '';
    const label = fieldLabels[field] ?? (field === 'confirmed_fields' ? '票据信息' : '报销信息');
    switch (type) {
      case 'missing': case 'string_too_short': return [`${label}不能为空，请填写后重试。`];
      case 'string_too_long': return [`${label}填写过长，请缩短后重试。`];
      case 'greater_than': return [`${label}必须大于 0，请核对填写的金额。`];
      case 'greater_than_equal': return [`${label}低于允许的最小值，请核对后重试。`];
      case 'less_than_equal': return [`${label}超过允许的最大值，请核对后重试。`];
      case 'decimal_parsing': case 'decimal_type': case 'int_parsing': case 'int_type':
      case 'finite_number': case 'decimal_max_digits': case 'decimal_max_places':
        return [`${label}格式不正确，请填写有效数字；金额最多保留两位小数。`];
      default:
        if (typeof type === 'string' && type.startsWith('date_')) return [`${label}无效，请填写有效的年月日。`];
        // Show intentional Chinese domain messages, not raw payloads or English internals.
        if (type === 'value_error' && typeof message === 'string' && message.startsWith('Value error, ')) {
          const text = message.slice('Value error, '.length);
          if (/[\u4e00-\u9fff]/.test(text)) return [`${label}：${text}。`];
        }
        return [`${label}格式或内容不符合要求，请核对后重试。`];
    }
  });
  return [...new Set(messages)].join(' ') || '表单校验未通过，请检查报销信息和票据字段。';
}
