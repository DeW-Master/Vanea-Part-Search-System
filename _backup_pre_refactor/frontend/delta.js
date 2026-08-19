// static/delta.js — Delta 视图前端逻辑 (PN+ZGS组合对比 + 下钻对比)
var deltaState = {
  page: 1,
  pageSize: 50,
  total: 0,
  totalPages: 0,
  loading: false,
  loaded: false,
  fromLabel: '',
  toLabel: ''
};

function loadDelta(page) {
  if (page) deltaState.page = page;
  if (deltaState.loading) return;
  deltaState.loading = true;

  var fromStage = document.getElementById('deltaFromStage').value;
  var toStage = document.getElementById('deltaToStage').value;
  var pnSearch = document.getElementById('deltaPnSearch').value.trim();
  deltaState.fromLabel = fromStage;
  deltaState.toLabel = toStage;

  var filters = [];
  var checkboxes = document.querySelectorAll('.filter-chip input:checked');
  for (var i = 0; i < checkboxes.length; i++) {
    filters.push(checkboxes[i].value);
  }

  var url = '/api/delta?from=' + fromStage + '&to=' + toStage +
            '&page=' + deltaState.page + '&page_size=' + deltaState.pageSize;
  if (pnSearch) url += '&pn=' + encodeURIComponent(pnSearch);
  for (var j = 0; j < filters.length; j++) {
    url += '&filter=' + filters[j];
  }

  document.getElementById('deltaList').innerHTML =
    '<p style="color:var(--text-secondary);text-align:center;padding:40px;">加载中...</p>';

  fetch(url)
    .then(function(r) { return r.json(); })
    .then(function(res) {
      deltaState.loading = false;
      if (!res.success) {
        document.getElementById('deltaList').innerHTML =
          '<p style="color:var(--danger);padding:20px;">' + (res.error || '查询失败') + '</p>';
        document.getElementById('deltaSummary').innerHTML = '';
        document.getElementById('deltaPagination').innerHTML = '';
        return;
      }
      renderDeltaSummary(res.data.summary);
      renderDeltaList(res.data.deltas);
      renderDeltaPagination(res.data);
    })
    .catch(function(err) {
      deltaState.loading = false;
      document.getElementById('deltaList').innerHTML =
        '<p style="color:var(--danger);padding:20px;">加载失败: ' + err + '</p>';
    });
}

function renderDeltaSummary(summary) {
  var items = [
    {num: summary.total_records, label: getDeltaLabel('deltaTotalRecords', '总变更记录'), color: 'var(--silver)'},
    {num: summary.zgs_upgraded || 0, label: getDeltaLabel('deltaZgsUpgraded', 'ZGS升级'), color: '#00d4ff'},
    {num: summary.new_parts || 0, label: getDeltaLabel('deltaNewParts', '新增零件'), color: '#00ff88'},
    {num: summary.discontinued_parts || 0, label: getDeltaLabel('deltaDiscontinued', 'PN停用'), color: '#ff6b6b'},
    {num: summary.ec_added || 0, label: getDeltaLabel('deltaEcAdded', 'EC新增'), color: '#ffd700'},
  ];
  document.getElementById('deltaSummary').innerHTML = items.map(function(i) {
    return '<div class="delta-stat-card" style="border-top:3px solid ' + i.color + ';">' +
           '<div class="num" style="color:' + i.color + ';">' + i.num + '</div>' +
           '<div class="label">' + i.label + '</div>' +
           '</div>';
  }).join('');
}

function renderDeltaList(deltas) {
  if (!deltas || deltas.length === 0) {
    document.getElementById('deltaList').innerHTML =
      '<p style="color:var(--text-secondary);text-align:center;padding:40px;">' +
      getDeltaLabel('deltaNoRecords', '无 Delta 记录') + '</p>';
    return;
  }

  var matchLabels = {
    'zgs_upgraded': getDeltaLabel('deltaMatchZgsUpgraded', 'ZGS升级'),
    'new_part': getDeltaLabel('deltaMatchNewPart', '新增'),
    'discontinued_part': getDeltaLabel('deltaMatchDiscontinued', 'PN停用'),
  };
  var matchColors = {
    'zgs_upgraded': '#00d4ff',
    'new_part': '#00ff88',
    'discontinued_part': '#ff6b6b',
  };

  var fromLabel = deltaState.fromLabel || getDeltaLabel('deltaFromStage', '前阶段');
  var toLabel = deltaState.toLabel || getDeltaLabel('deltaToStage', '后阶段');

  var html = deltas.map(function(d) {
    var changedFields = d.changes.filter(function(c) {
      return c.change_type !== 'unchanged' && c.change_type !== 'unavailable';
    });
    var changeTags = changedFields.map(function(c) {
      var color = matchColors[d.match_type] || 'var(--silver)';
      return '<span class="change-tag ' + c.change_type +
             '" style="border-color:' + color + '33;">' +
             c.business + ': ' + changeTypeLabel(c.change_type) + '</span>';
    }).join('');

    var matchLabel = matchLabels[d.match_type] || d.match_type;
    var matchColor = matchColors[d.match_type] || 'var(--silver)';

    var fieldRows = d.changes.map(function(c) {
      return '<div class="delta-field-row">' +
             '<div class="field-label">' + c.business + '</div>' +
             '<div class="old-val ' + (c.old_value ? '' : 'empty') + '">' + (c.old_value || '—') + '</div>' +
             '<div class="arrow ' + c.change_type + '">' + changeTypeLabel(c.change_type) + '</div>' +
             '<div class="new-val ' + (c.new_value ? '' : 'empty') + '">' + (c.new_value || '—') + '</div>' +
             '</div>';
    }).join('');

    return '<div class="delta-item" onclick="toggleDeltaExpand(this)">' +
           '<div class="delta-item-header">' +
           '<div style="display:flex;align-items:center;gap:10px;">' +
           '<span class="pn" style="cursor:pointer;color:' + matchColor + ';" ' +
           'onclick="event.stopPropagation();openDeltaDetail(\'' + d.part_number + '\')" ' +
           'title="点击查看详情">' + d.part_number + '</span>' +
           '<span class="match-badge" style="background:' + matchColor + '22;color:' + matchColor +
           ';border:1px solid ' + matchColor + '55;">' + matchLabel + '</span>' +
           '</div>' +
           '<div class="change-types">' + changeTags + '</div>' +
           '</div>' +
           '<div class="delta-item-body">' +
           '<div class="delta-field-row" style="font-weight:700;border-bottom:2px solid var(--border);">' +
           '<div>' + getDeltaLabel('deltaFieldCol', '字段') + '</div>' +
           '<div>' + fromLabel + '</div>' +
           '<div>' + getDeltaLabel('deltaChangeCol', '变化') + '</div>' +
           '<div>' + toLabel + '</div>' +
           '</div>' +
           fieldRows +
           '<div style="margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);">' +
           '<button class="btn-primary" style="font-size:12px;padding:4px 12px;" ' +
           'onclick="event.stopPropagation();openDeltaDetail(\'' + d.part_number + '\')">' +
           getDeltaLabel('deltaViewDetail', '查看详情') + '</button>' +
           '</div>' +
           '</div>' +
           '</div>';
  }).join('');

  document.getElementById('deltaList').innerHTML = html;
}

function toggleDeltaExpand(el) {
  el.classList.toggle('expanded');
}

// ===== 下钻查看详情（两阶段对比 + 差异高亮） =====
function openDeltaDetail(partNumber) {
  var fromStage = document.getElementById('deltaFromStage').value;
  var toStage = document.getElementById('deltaToStage').value;

  // 创建或复用弹窗
  var modal = document.getElementById('deltaDetailModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'deltaDetailModal';
    modal.style.cssText = 'display:flex;position:fixed;top:0;left:0;width:100%;height:100%;' +
      'background:rgba(0,0,0,0.75);z-index:10000;justify-content:center;align-items:center;' +
      'backdrop-filter:blur(4px);';
    modal.onclick = function(e) { if (e.target === modal) closeDeltaDetail(); };
    document.body.appendChild(modal);
  }

  modal.innerHTML =
    '<div style="background:var(--card-bg);border:1px solid var(--border);border-radius:14px;' +
    'max-width:90vw;max-height:88vh;width:960px;display:flex;flex-direction:column;overflow:hidden;' +
    'box-shadow:0 20px 60px rgba(0,0,0,0.5);">' +
    '<div style="display:flex;justify-content:space-between;align-items:center;padding:18px 24px;' +
    'border-bottom:1px solid var(--border);background:linear-gradient(180deg,rgba(212,175,55,0.05),transparent);">' +
    '<div>' +
    '<h3 style="margin:0;color:var(--silver);font-size:18px;font-weight:300;letter-spacing:0.5px;">' +
    getDeltaLabel('deltaDetailTitle', '零件差异详情') + '</h3>' +
    '<div style="margin-top:4px;font-size:13px;color:var(--text-secondary);">' +
    '<span style="color:#ff6b6b;">' + fromStage + '</span>' +
    ' <span style="margin:0 6px;">→</span> ' +
    '<span style="color:#00ff88;">' + toStage + '</span>' +
    ' · PN: <strong style="color:var(--silver);">' + partNumber + '</strong>' +
    '</div>' +
    '</div>' +
    '<button onclick="closeDeltaDetail()" style="background:none;border:none;' +
    'color:var(--text-secondary);font-size:28px;cursor:pointer;line-height:1;' +
    'padding:0 8px;">&times;</button>' +
    '</div>' +
    '<div id="deltaDetailContent" style="padding:20px 24px;overflow:auto;flex:1;">' +
    '<p style="color:var(--text-secondary);text-align:center;padding:30px;">' +
    getDeltaLabel('deltaLoading', '加载中...') + '</p>' +
    '</div>' +
    '</div>';
  modal.style.display = 'flex';

  // 拉取对比数据
  fetch('/api/delta_detail?pn=' + encodeURIComponent(partNumber) +
        '&from=' + fromStage + '&to=' + toStage)
    .then(function(r) { return r.json(); })
    .then(function(res) {
      if (!res.success) {
        document.getElementById('deltaDetailContent').innerHTML =
          '<p style="color:var(--danger);text-align:center;">' + (res.error || '查询失败') + '</p>';
        return;
      }
      renderDeltaDetailContent(res.data, fromStage, toStage);
    })
    .catch(function(err) {
      document.getElementById('deltaDetailContent').innerHTML =
        '<p style="color:var(--danger);text-align:center;">加载失败: ' + err + '</p>';
    });
}

function closeDeltaDetail() {
  var modal = document.getElementById('deltaDetailModal');
  if (modal) modal.style.display = 'none';
}

function renderDeltaDetailContent(data, fromStage, toStage) {
  var comp = data.comparison;
  var content = document.getElementById('deltaDetailContent');

  if (!comp) {
    content.innerHTML = '<p style="color:var(--text-secondary);text-align:center;">无对比数据</p>';
    return;
  }

  var html = '';

  // 差异统计条
  var diffCount = comp.total_differences || 0;
  html += '<div style="display:flex;align-items:center;justify-content:space-between;' +
    'margin-bottom:16px;padding:12px 16px;background:rgba(0,212,255,0.08);' +
    'border:1px solid rgba(0,212,255,0.3);border-radius:8px;">' +
    '<div style="display:flex;align-items:center;gap:10px;">' +
    '<span style="font-size:24px;font-weight:700;color:#00d4ff;">' + diffCount + '</span>' +
    '<span style="color:var(--text-secondary);">' +
    getDeltaLabel('deltaDiffCount', '处差异') + '</span>' +
    '</div>' +
    '<div style="font-size:12px;color:var(--text-secondary);">' +
    (comp.from_exists ? '' : (fromStage + ' 无数据 · ')) +
    (comp.to_exists ? '' : (toStage + ' 无数据')) +
    '</div>' +
    '</div>';

  // 核心字段对比表
  if (comp.fields && comp.fields.length > 0) {
    html += '<div style="margin-bottom:20px;">' +
      '<div style="font-size:13px;font-weight:600;color:var(--silver);margin-bottom:8px;' +
      'letter-spacing:0.5px;">' + getDeltaLabel('deltaCoreFields', '核心字段对比') + '</div>' +
      renderCompareTable(comp.fields, fromStage, toStage) +
      '</div>';
  }

  // 额外差异字段
  if (comp.extra_fields && comp.extra_fields.length > 0) {
    html += '<div>' +
      '<div style="font-size:13px;font-weight:600;color:var(--silver);margin-bottom:8px;' +
      'letter-spacing:0.5px;">' + getDeltaLabel('deltaExtraFields', '其他差异字段') +
      ' (' + comp.extra_fields.length + ')</div>' +
      renderCompareTable(comp.extra_fields, fromStage, toStage) +
      '</div>';
  }

  // 导出按钮
  html += '<div style="margin-top:20px;text-align:right;">' +
    '<button class="btn-primary" style="font-size:13px;padding:6px 16px;" ' +
    'onclick="exportDeltaDetailCSV(\'' + data.part_number + '\')">' +
    getDeltaLabel('deltaExportCSV', '导出CSV') + '</button>' +
    '</div>';

  content.innerHTML = html;
  window._deltaDetailData = data;
}

function renderCompareTable(fields, fromStage, toStage) {
  var changeColors = {
    'added': '#00ff88',
    'removed': '#ff6b6b',
    'changed': '#ffd700',
    'upgraded': '#00d4ff',
    'persisted': 'var(--text-secondary)',
    'unchanged': 'var(--text-secondary)',
  };

  var rows = fields.map(function(f) {
    var isDiff = f.is_different;
    var color = changeColors[f.change_type] || 'var(--text-secondary)';
    var rowBg = isDiff ? 'rgba(255,215,0,0.06)' : 'transparent';
    var borderStyle = isDiff ? '1px solid rgba(255,215,0,0.3)' : '1px solid var(--border)';

    return '<tr style="background:' + rowBg + ';">' +
      '<td style="padding:8px 12px;' + borderStyle + ';font-size:13px;' +
      'color:var(--text-secondary);width:180px;">' + f.business + '</td>' +
      '<td style="padding:8px 12px;' + borderStyle + ';font-size:13px;font-family:monospace;' +
      (isDiff && f.from_value ? 'background:rgba(255,107,107,0.1);color:#ff9999;' : 'color:var(--text-primary);') +
      '">' + (f.from_value || '<span style="color:var(--text-tertiary);">—</span>') + '</td>' +
      '<td style="padding:8px 12px;' + borderStyle + ';text-align:center;font-size:11px;' +
      'color:' + color + ';font-weight:600;white-space:nowrap;width:80px;">' +
      changeTypeLabel(f.change_type) + '</td>' +
      '<td style="padding:8px 12px;' + borderStyle + ';font-size:13px;font-family:monospace;' +
      (isDiff && f.to_value ? 'background:rgba(0,255,136,0.1);color:#66ffaa;' : 'color:var(--text-primary);') +
      '">' + (f.to_value || '<span style="color:var(--text-tertiary);">—</span>') + '</td>' +
      '</tr>';
  }).join('');

  return '<table style="width:100%;border-collapse:collapse;">' +
    '<thead>' +
    '<tr style="background:rgba(255,255,255,0.03);">' +
    '<th style="padding:10px 12px;border:1px solid var(--border);text-align:left;' +
    'font-size:12px;color:var(--text-secondary);font-weight:600;letter-spacing:0.5px;">字段</th>' +
    '<th style="padding:10px 12px;border:1px solid var(--border);text-align:left;' +
    'font-size:12px;color:#ff6b6b;font-weight:600;">' + fromStage + '</th>' +
    '<th style="padding:10px 12px;border:1px solid var(--border);text-align:center;' +
    'font-size:12px;color:var(--text-secondary);font-weight:600;">变化</th>' +
    '<th style="padding:10px 12px;border:1px solid var(--border);text-align:left;' +
    'font-size:12px;color:#00ff88;font-weight:600;">' + toStage + '</th>' +
    '</tr>' +
    '</thead>' +
    '<tbody>' + rows + '</tbody>' +
    '</table>';
}

function exportDeltaDetailCSV(partNumber) {
  var data = window._deltaDetailData;
  if (!data || !data.comparison) return;
  var comp = data.comparison;
  var allFields = (comp.fields || []).concat(comp.extra_fields || []);

  var csv = '\uFEFF';
  csv += 'Field,' + comp.from_stage + ',Change Type,' + comp.to_stage + '\n';
  allFields.forEach(function(f) {
    var row = [
      '"' + String(f.business).replace(/"/g, '""') + '"',
      '"' + String(f.from_value || '').replace(/"/g, '""') + '"',
      f.change_type,
      '"' + String(f.to_value || '').replace(/"/g, '""') + '"',
    ];
    csv += row.join(',') + '\n';
  });

  var blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  var link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'delta_detail_' + partNumber + '.csv';
  link.click();
  URL.revokeObjectURL(link.href);
}

function changeTypeLabel(ct) {
  var lang = (typeof currentLang !== 'undefined') ? currentLang : 'zh';
  var labelsZh = {
    'added': '新增', 'changed': '变更', 'upgraded': '升级',
    'persisted': '持续', 'removed': '清除', 'resolved': '已解决',
    'unchanged': '无变化', 'unavailable': 'N/A'
  };
  var labelsEn = {
    'added': 'Added', 'changed': 'Changed', 'upgraded': 'Upgraded',
    'persisted': 'Persisted', 'removed': 'Removed', 'resolved': 'Resolved',
    'unchanged': 'Unchanged', 'unavailable': 'N/A'
  };
  var labels = lang === 'en' ? labelsEn : labelsZh;
  return labels[ct] || ct;
}

function getDeltaLabel(key, fallback) {
  if (typeof i18n !== 'undefined' && typeof currentLang !== 'undefined' &&
      i18n[currentLang] && i18n[currentLang][key]) {
    return i18n[currentLang][key];
  }
  return fallback;
}

function renderDeltaPagination(data) {
  deltaState.total = data.total;
  deltaState.totalPages = data.total_pages;
  var el = document.getElementById('deltaPagination');
  if (data.total_pages <= 1) { el.innerHTML = ''; return; }
  var prevLabel = getDeltaLabel('deltaPrevPage', '上一页');
  var nextLabel = getDeltaLabel('deltaNextPage', '下一页');
  el.innerHTML =
    '<button ' + (data.page <= 1 ? 'disabled' : '') +
    ' onclick="loadDelta(' + (data.page - 1) + ')">' + prevLabel + '</button>' +
    '<span class="page-info">' + data.page + ' / ' + data.total_pages +
    ' (共' + data.total + '条)</span>' +
    '<button ' + (data.page >= data.total_pages ? 'disabled' : '') +
    ' onclick="loadDelta(' + (data.page + 1) + ')">' + nextLabel + '</button>';
}

// 筛选变化时自动查询
document.addEventListener('change', function(e) {
  if (e.target.closest && e.target.closest('.filter-chip')) {
    loadDelta(1);
  }
});

// ESC键关闭弹窗
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeDeltaDetail();
});

// 回车搜索
document.addEventListener('DOMContentLoaded', function() {
  var pnInput = document.getElementById('deltaPnSearch');
  if (pnInput) {
    pnInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter') {
        loadDelta(1);
      }
    });
  }
});
