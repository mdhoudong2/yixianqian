const { createApp, ref, computed, onMounted } = Vue;

// anti-copy: disable text selection and long-press save on images
(function(){
  try{
    function isEditable(el){
      try{
        if(!el) return false;
        if(el.tagName==='INPUT'||el.tagName==='TEXTAREA'||el.isContentEditable) return true;
        if(el.closest && el.closest('input, textarea, [contenteditable="true"], [contenteditable=""]')) return true;
        var p=el.parentElement;
        while(p){
          if(p.tagName==='INPUT'||p.tagName==='TEXTAREA'||p.isContentEditable) return true;
          p=p.parentElement;
        }
      }catch(e){}
      return false;
    }
    document.addEventListener('contextmenu', function(e){ if(isEditable(e.target)) return; e.preventDefault(); });
    document.addEventListener('selectstart', function(e){ if(isEditable(e.target)) return; e.preventDefault(); });
    document.addEventListener('copy', function(e){ if(isEditable(e.target)) return; e.preventDefault(); });
    document.addEventListener('cut', function(e){ if(isEditable(e.target)) return; e.preventDefault(); });
    document.addEventListener('dragstart', function(e){ e.preventDefault(); });
    function disableImgDrag(){
      document.querySelectorAll('img').forEach(function(img){ img.setAttribute('draggable','false'); });
    }
    if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded', disableImgDrag); } else { disableImgDrag(); }
    new MutationObserver(disableImgDrag).observe(document.documentElement,{childList:true,subtree:true});
  }catch(e){}
})();

// toggle active users display (temporarily disabled, set true to restore)
const SHOW_ACTIVE_USERS = false;

const TEXT = {
  heroTitle: '\u4e00\u7ebf\u7275',
  heroSub: '\u4e3a\u6559\u4f1a\u5185\u672a\u5a5a\u9752\u5e74\u7275\u7ebf\u642d\u6865<br>\u613f\u4e3b\u5185\u7684\u5144\u5f1f\u59d0\u59b9\uff0c\u65e9\u65e5\u627e\u5230\u53e6\u4e00\u534a',
  f1Title: '\u4eba\u5de5\u5ba1\u6838',
  f1Desc: '\u6bcf\u4e00\u4efd\u8d44\u6599\u90fd\u7ecf\u8fc7\u4eba\u5de5\u5ba1\u6838\uff0c\u62d2\u7edd\u865a\u5047',
  f2Title: '\u533f\u540d\u559c\u6b22',
  f2Desc: '\u70b9\u7231\u5fc3\uff0c\u533f\u540d\u559c\u6b22\uff0c\u76f8\u4e92\u559c\u6b22\u5c31\u81ea\u52a8\u7275\u7ebf',
  f3Title: '\u52a8\u6001\u901a\u77e5',
  f3Desc: '\u4f60\u559c\u6b22\u7684\u4eba\u62a5\u540d\u4e86\u6d3b\u52a8\uff0c\u4f60\u5c31\u4f1a\u6536\u5230\u901a\u77e5',
  maleTitle: '\u6d3b\u8dc3\u7684\u7537\u751f\u4eec',
  femaleTitle: '\u6d3b\u8dc3\u7684\u5973\u751f\u4eec',
  joinBtn: '\u5982\u4f55\u6ce8\u518c',
  guideTitle: '\u52a0\u5165\u4e00\u7ebf\u7275 · \u4e24\u6b65\u5b8c\u6210',
  step1a: '\u4e0b\u8f7d ',
  feishuApp: '\u98de\u4e66 App',
  step1b: ' \u5e76\u6ce8\u518c',
  step1c: '\uff08\u5404\u624b\u673a\u5e94\u7528\u5e02\u573a\u5747\u53ef\u4e0b\u8f7d\uff09',
  feishuAlt: '\u98de\u4e66App\u56fe\u6807',
  step2a: '\u70b9\u51fb\u4e0b\u9762\u94fe\u63a5\uff0c\u9009\u62e9 ',
  step2b: '\u300c\u98de\u4e66\u300d',
  step2c: ' \u4e2d\u6253\u5f00\uff0c\u70b9\u51fb ',
  step2d: '\u300c\u7533\u8bf7\u4f7f\u7528 → \u5f00\u542f\u5355\u804a\u300d',
  step2e: '\uff1a',
  orA: '\u6216\u8005\uff0c\u7528\u98de\u4e66\u626b\u63cf\u4e0b\u65b9\u4e8c\u7ef4\u7801\uff0c\u70b9\u51fb ',
  qrAlt: '\u4e00\u7ebf\u7275\u4e8c\u7ef4\u7801',
  qrHint: '\u6253\u5f00\u98de\u4e66\u300c\u626b\u4e00\u626b\u300d\u8bc6\u522b\u4e8c\u7ef4\u7801',
  okBtn: '\u6211\u77e5\u9053\u4e86',
  genderMale: '\u7537\u6027',
  genderFemale: '\u5973\u6027',
  cityLabel: '\u73b0\u5c45/\u5de5\u4f5c\u57ce\u5e02'
};

createApp({
  setup() {
    const showGuide = ref(false);
    const users = ref([]);

    const maleUsers = computed(() => users.value.filter(u => u.gender === TEXT.genderMale));
    const femaleUsers = computed(() => users.value.filter(u => u.gender === TEXT.genderFemale));

    function simpleLine(u) {
      return (u.display_fields && u.display_fields.simple) || '';
    }
    function cityOf(u) {
      const df = u.display_fields || {};
      const sections = df.sections || [];
      for (const sec of sections) {
        const f = (sec.fields || []).find(x => x.label === TEXT.cityLabel);
        if (f) return f.value;
      }
      return '';
    }

    function track(event) {
      try {
        fetch('/api/track', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ event, from: 'public' })
        }).catch(() => {});
      } catch (e) {}
    }
    function onJoin() {
      showGuide.value = true;
      track('join_click');
    }

    async function loadUsers() {
      try {
        const resp = await fetch('/api/public/users');
        const data = await resp.json();
        users.value = data.users || [];
      } catch (e) {}
    }

    onMounted(() => {
      track('page_view');
      if (SHOW_ACTIVE_USERS) {
        loadUsers();
        setInterval(loadUsers, 30000);
      }
    });

    return { t: TEXT, showGuide, users, maleUsers, femaleUsers, simpleLine, cityOf, onJoin, showActiveUsers: SHOW_ACTIVE_USERS };
  }
}).use(vant).mount('#app');
