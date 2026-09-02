/* 一线牵 H5 原生注册页（复刻「一线牵-注册（新）」表单） */
const { createApp, ref, reactive, onMounted } = Vue;

// ===== 选项常量（与多维表格单/多选选项严格一致）=====
const TRAITS = ["喜欢安静","比较主动","非常自律","做事严谨","性格沉稳","积极乐观","有始有终","喜爱冒险","猎奇","渴望成功","理性待事","正直","话痨","善于交际","容易相处","有点小幽默","传统的","思想前卫","总是充满热情","讲究效率","其他"];
const HOBBIES = ["有氧运动","听音乐","看电影","看书","手绘","唱歌","弹吉他","弹钢琴","剧本杀","狼人杀","王者荣耀","吃鸡","密室逃脱","爱车一族","旅游爱好者","看小说","拍照片","养宠物","做饭","寻觅美食","逛博物馆","游乐场","蹦迪","其他"];
const SPORTS = ["篮球","足球","乒乓球","羽毛球","网球","台球","游泳","跑步","爬山","射击","跳绳","漂流","瑜伽","慢走","跳舞","骑行","攀岩","蹦床","滑雪","冲浪","滑冰","跆拳道","蹦极","极限运动","潜水","其他"];
const MBTI = ["E","I","S","N","T","F","J","P"];

// 小天使确认项字段名（与多维表格一致，含全角引号）
const ANGEL_KEY = "记得一定要加小天使👼微信号，并备注“真实姓名”。";

// ===== 字段定义（顺序/文案/类型严格对齐飞书表单）=====
// type: photo/text/tel/number/date/textarea/select/radio/multi/area/confirm
const FIELDS = [
  { n:1,  key:"__photo__", label:"个人照片", type:"photo", required:true,
    hint:"请上传 3-9 张个人照片，建议上传“横版”照片作为第一张，展示效果更好" },
  { n:2,  key:"姓名", label:"姓名", type:"text", required:true, hint:"不公开，请务必填写“真实姓名”" },
  { n:3,  key:"昵称", label:"昵称", type:"text", required:true, hint:"会公开，希望大家怎么喊你，如果介意就起一个不常用的" },
  { n:4,  key:"身份证号", label:"身份证号", type:"text", required:true, hint:"不公开，后期活动期间，用于购买保险，请如实填写" },
  { n:5,  key:"性别", label:"性别", type:"select", required:true, options:["男性","女性"] },
  { n:6,  key:"生日", label:"生日", type:"date", required:true },
  { n:7,  key:"手机号", label:"手机号", type:"tel", required:true, hint:"不公开" },
  { n:8,  key:"微信号", label:"微信号", type:"text", required:true, hint:"不公开，仅相互喜欢后可见" },
  { n:9,  key:"圣名", label:"圣名", type:"text", required:true },
  { n:10, key:"经常去的教堂", label:"经常去的教堂", type:"text", required:true, hint:"例如，深圳南头堂" },
  { n:11, key:"参加的团体", label:"参加的团体", type:"text", required:false, hint:"例如，圣咏团、服务组" },
  { n:12, key:"教堂所在市-区", label:"教堂所在城市", type:"area", levels:3, required:true, hint:"例如，深圳南山" },
  { n:13, key:"家乡", label:"家乡", type:"area", levels:2, required:true, hint:"例如，广东广州" },
  { n:14, key:"家庭成员情况", label:"家庭成员情况", type:"textarea", required:true, hint:"几个兄弟姐妹等" },
  { n:15, key:"现居/工作城市", label:"现居/工作城市", type:"area", levels:3, required:true, hint:"只填写省、市；例如，广东深圳" },
  { n:16, key:"身高（cm）", label:"身高（cm）", type:"number", required:true, placeholder:"请输入整数" },
  { n:17, key:"学历", label:"学历", type:"select", required:true, options:["大专以下","大专","本科","硕士","博士"] },
  { n:18, key:"从事行业", label:"从事行业", type:"text", required:true, hint:"如果自己做生意，请注明" },
  { n:19, key:"职位", label:"职位", type:"text", required:true, hint:"如果自己做生意，请注明" },
  { n:20, key:"房产状况", label:"房产状况", type:"radio", required:false, options:["有","无"],
    hint:"1、有的小伙伴要求了解房产和收入状况，如果方便，请如实填写。如果感觉有困难，可不填写。\n2、如有房子，请注明城市，平米数。\n3、此信息会公开。",
    note:{"有":"房产状况-有-补充内容","无":"房产状况-无-补充内容"}, notePlaceholder:"请补充描述（城市、平米数）" },
  { n:21, key:"年收入", label:"年收入", type:"select", required:false, hint:"理由同上",
    options:["10W以下","10W - 20W","20W - 30W","30W - 50W","50W以上"] },
  { n:22, key:"是否有车", label:"是否有车", type:"radio", required:false, options:["有","无"], hint:"理由同上",
    note:{"有":"是否有车-有-补充内容","无":"是否有车-无-补充内容"}, notePlaceholder:"请补充描述" },
  { n:23, key:"我是一个怎样的人", label:"我是一个怎样的人", type:"textarea", required:true, example:true,
    hint:"看看别人怎么写：我目前是一名大厂的项目经理，我的家乡是内蒙古乌兰察布，因为在北京上学多年，在深圳买了房，深圳算是我的第二故乡。性格方面是典型的摩羯男性格，踏实稳重，具有责任感，懂得知足，懂得感恩。外冷内热，在熟人面前就是一个沙雕，在恋人面前有时幼稚又沙雕。喜欢运动，每周至少去两次健身房，喜欢旅行和拍照。周末比较喜欢去教堂玩，参加了南头堂乐队，会弹吉他。" },
  { n:24, key:"我是一个怎样的人-性格", label:"我是一个怎样的人-性格", type:"multi", required:true, options:TRAITS, hint:"可多选",
    other:"我是一个怎样的人-性格-其他-补充内容" },
  { n:25, key:"我是一个怎样的人-爱好", label:"我是一个怎样的人-爱好", type:"multi", required:true, options:HOBBIES, hint:"可多选",
    other:"我是一个怎样的人-爱好-其他-补充内容" },
  { n:26, key:"我是一个怎样的人-运动", label:"我是一个怎样的人-运动", type:"multi", required:true, options:SPORTS, hint:"可多选",
    other:"我是一个怎样的人-运动-其他-补充内容" },
  { n:27, key:"我是个怎样的人-MBTI人格", label:"我是个怎样的人-MBTI人格", type:"multi", required:true, options:MBTI,
    hint:"一般选4个：\n- 内向还是外向？ E（外向）/ I (内向)；\n- 你更注重什么？ S (实感) / N (直觉)；\n- 你如何做决定？ T (思考) / F (情感)；\n- 你更喜欢怎样的生活方式？ J（有计划、有条理）/ P（灵活、开放）；" },
  { n:28, key:"理想中的TA", label:"我理想中的TA", type:"textarea", required:true, example:true,
    hint:"看看别人怎么写：希望你善良一些，可爱一些，温柔大方，善解人意。希望你有自己的爱好和伙伴，追求向上，愿意一起把日子过得越来越好。希望情侣之间的关系能够是轻松、愉快的、相互扶持的。希望我们一起去教堂，一起服务，一起参加乐队赞美天主。" },
  { n:29, key:"理想中的TA-性格", label:"理想中的TA-性格", type:"multi", required:true, options:TRAITS, hint:"可多选",
    other:"理想中的TA-性格-其他-补充内容" },
  { n:30, key:"理想中的TA-爱好", label:"理想中的TA-爱好", type:"multi", required:true, options:HOBBIES, hint:"可多选",
    other:"理想中的TA-爱好-其他-补充内容" },
  { n:31, key:"理想中的TA-运动", label:"理想中的TA-运动", type:"multi", required:true, options:SPORTS, hint:"可多选",
    other:"理想中的TA-运动-其他-补充内容" },
  { n:32, key:"婚后是否与父母同住", label:"婚后是否与父母同住", type:"select", required:true,
    options:["独立生活","与父母住在一起","根据具体情况而定"] },
  { n:33, key:"你结过婚吗？", label:"你结过婚吗？", type:"select", required:true, options:["没结过婚","结过婚"],
    hint:"不公开；结婚：领结婚证、举行婚礼圣事，占其一就算结婚。" },
  { n:34, key:"您替子女注册吗？", label:"您替子女注册吗？", type:"select", required:true,
    options:["是的，我替子女报名，我会让子女注册","不是，我为自己报名"],
    hint:"如果您提子女注册，请使用子女手机号注册，因为后期要发送消息，这样子子女才能接收到" },
  { n:35, key:"你是怎么知道这个App的？", label:"你是怎么知道一线牵App的？", type:"textarea", required:true },
  { n:36, key:"邀请人ID", label:"邀请人ID", type:"text", required:false },
  { n:37, key:ANGEL_KEY, label:"记得一定要加小天使👼微信号，并备注“真实姓名”。", type:"confirm", required:true,
    hint:"加小天使请备注好“真实姓名”；可以先长按二维码，保存到手机；" },
];

const FEISHU_APP_ID = window.YXQ_FEISHU_APP_ID || "";

createApp({
  setup() {
    const stage = ref("loading");           // loading | form | exists | done
    const submitting = ref(false);
    const existsNick = ref("");
    const photos = ref([]);                 // [{preview, token, uploading}]
    const errors = reactive({});
    const form = reactive({});
    const area = reactive({});

    // 初始化所有字段值 + 条件补充字段
    FIELDS.forEach(f => {
      if (f.type === "multi") form[f.key] = [];
      else if (f.type === "area") area[f.key] = { province:"", city:"", county:"" };
      else form[f.key] = "";
      if (f.note) Object.values(f.note).forEach(k => form[k] = "");
      if (f.other) form[f.other] = "";
    });

    const AREA = window.YXQ_AREA || {};
    const provinces = Object.keys(AREA);
    function cityList(key) { const p = area[key].province; return p && AREA[p] ? Object.keys(AREA[p]) : []; }
    function countyList(key) { const a = area[key]; return (a.province && a.city && AREA[a.province]) ? (AREA[a.province][a.city] || []) : []; }
    function onAreaProvince(key) { area[key].city = ""; area[key].county = ""; }
    function onAreaCity(key) { area[key].county = ""; }

    function pickRadio(key, opt) { form[key] = (form[key] === opt ? "" : opt); }
    function toggleMulti(key, opt) {
      const arr = form[key] || [];
      const i = arr.indexOf(opt);
      if (i >= 0) arr.splice(i, 1); else arr.push(opt);
    }
    function toggleConfirm(key) { form[key] = form[key] === "我会记得的" ? "" : "我会记得的"; }

    // ===== 照片选择与上传 =====
    function onPickPhotos(ev) {
      const files = Array.from(ev.target.files || []);
      ev.target.value = "";
      files.forEach(file => {
        if (photos.value.length >= 9) return;
        const item = { preview: URL.createObjectURL(file), token: null, uploading: true };
        photos.value.push(item);
        const fd = new FormData();
        fd.append("file", file);
        fetch("/api/register/photo", { method:"POST", credentials:"same-origin",
          headers:{ "X-Requested-With":"XMLHttpRequest" }, body:fd })
          .then(r => r.json()).then(d => {
            item.uploading = false;
            if (d.ok) item.token = d.token;
            else { vant.showToast(d.error || "图片上传失败"); removePhoto(photos.value.indexOf(item)); }
          }).catch(() => { item.uploading = false; vant.showToast("图片上传失败，请重试"); removePhoto(photos.value.indexOf(item)); });
      });
    }
    function removePhoto(i) { if (i >= 0) photos.value.splice(i, 1); }

    // ===== 飞书身份 =====
    function isInFeishu() { const ua = navigator.userAgent.toLowerCase(); return ua.includes("lark") || ua.includes("feishu"); }
    function getSilentCode() {
      return new Promise(resolve => {
        let waited = 0;
        const tryGet = () => {
          const tt = window.tt || window.h5sdk;
          if (!tt) { if (waited < 3000) { waited += 200; setTimeout(tryGet, 200); return; } resolve(null); return; }
          if (typeof tt.requestAuthCode === "function") {
            tt.requestAuthCode({ appId: FEISHU_APP_ID,
              success: res => resolve(res && res.code ? res.code : null),
              fail: () => {
                if (typeof tt.requestAccess === "function") {
                  tt.requestAccess({ appID: FEISHU_APP_ID, scopeList:[],
                    success: r2 => resolve(r2 && r2.code ? r2.code : null), fail:()=>resolve(null) });
                } else resolve(null);
              }});
          } else if (typeof tt.requestAccess === "function") {
            tt.requestAccess({ appID: FEISHU_APP_ID, scopeList:[],
              success: r => resolve(r && r.code ? r.code : null), fail:()=>resolve(null) });
          } else resolve(null);
        };
        tryGet();
      });
    }
    function oauthRedirect() {
      const qs = new URLSearchParams(location.search);
      const inviter = qs.get("inviter") || "";
      const redirect = location.origin + "/register.html" + (inviter ? ("?inviter=" + encodeURIComponent(inviter)) : "");
      const url = "https://open.feishu.cn/open-apis/authen/v1/index?app_id=" +
        encodeURIComponent(FEISHU_APP_ID) + "&redirect_uri=" + encodeURIComponent(redirect);
      location.replace(url);
    }
    async function prepare(code) {
      let url = "/api/register/prepare";
      if (code) url += "?code=" + encodeURIComponent(code);
      const r = await fetch(url, { credentials:"same-origin", headers:{ "X-Requested-With":"XMLHttpRequest" } });
      return r.json();
    }
    async function boot() {
      const qs = new URLSearchParams(location.search);
      // 纯预览模式：仅展示空表单用于核对样式/文案，提交仍由后端强制飞书登录
      if (qs.get("preview") === "1") { stage.value = "form"; return; }
      const code = qs.get("code") || "";
      const inviter = qs.get("inviter") || "";
      try {
        let data = null;
        if (code) data = await prepare(code);
        else data = await prepare("");           // 先试已有会话
        if (data && data.ok) {
          if (data.stage === "exists") { existsNick.value = data.nickname || ""; stage.value = "exists"; return; }
          if (inviter && !form["邀请人ID"]) form["邀请人ID"] = inviter;
          stage.value = "form";
          return;
        }
      } catch (e) { /* 落到下面重新取 code */ }
      // 无有效会话：飞书内静默免登，外部浏览器跳 OAuth
      if (isInFeishu()) {
        const c = await getSilentCode();
        if (c) {
          try {
            const d = await prepare(c);
            if (d && d.ok) {
              if (d.stage === "exists") { existsNick.value = d.nickname||""; stage.value="exists"; return; }
              if (inviter && !form["邀请人ID"]) form["邀请人ID"] = inviter;
              stage.value = "form"; return;
            }
          } catch (e) {}
        }
        vant.showDialog({ title:"登录失败", message:"请从「一线牵」机器人重新进入注册页", confirmButtonColor:"#ff6b81" });
        stage.value = "form";  // 兜底展示（提交仍会被后端拦）
        return;
      }
      oauthRedirect();
    }

    // ===== 校验与提交 =====
    function validate() {
      Object.keys(errors).forEach(k => delete errors[k]);
      let firstErr = null;
      const setErr = (k, msg) => { errors[k] = msg; if (!firstErr) firstErr = k; };
      for (const f of FIELDS) {
        if (!f.required) continue;
        if (f.type === "photo") {
          const ready = photos.value.filter(p => p.token && !p.uploading);
          if (ready.length < 3) setErr("photo", "请至少上传 3 张照片");
          else if (photos.value.some(p => p.uploading)) setErr("photo", "图片还在上传中，请稍候");
          continue;
        }
        if (f.type === "area") {
          const a = area[f.key];
          if (!a.province || !a.city) { setErr(f.key, "请选择省、市"); continue; }
          if (f.levels === 3 && countyList(f.key).length && !a.county) { setErr(f.key, "请选择区/县"); continue; }
          form[f.key] = [a.province, a.city, a.county].filter(Boolean).join(" ");
          continue;
        }
        if (f.type === "multi") {
          if (!(form[f.key] || []).length) setErr(f.key, "请至少选择一项");
          continue;
        }
        if (f.type === "confirm") {
          if (form[f.key] !== "我会记得的") setErr(f.key, "请勾选确认");
          continue;
        }
        const v = (form[f.key] || "").toString().trim();
        if (!v) setErr(f.key, "此项为必填");
        if (f.type === "number" && v && !/^\d+$/.test(v)) setErr(f.key, "请输入整数");
        if (f.key === "身份证号" && v && !/^\d{17}[\dXx]$/.test(v)) setErr(f.key, "请输入正确的18位身份证号");
      }
      return !firstErr;
    }

    async function onSubmit() {
      if (submitting.value) return;
      if (!validate()) { vant.showToast("请完善标红的必填项"); return; }
      // 组装 area 文本（非必填场景也补齐）
      FIELDS.forEach(f => {
        if (f.type === "area") { const a = area[f.key]; form[f.key] = [a.province,a.city,a.county].filter(Boolean).join(" "); }
      });
      const tokens = photos.value.map(p => p.token).filter(Boolean);
      submitting.value = true;
      try {
        const r = await fetch("/api/register/submit", {
          method:"POST", credentials:"same-origin",
          headers:{ "Content-Type":"application/json", "X-Requested-With":"XMLHttpRequest" },
          body: JSON.stringify({ fields: JSON.parse(JSON.stringify(form)), photos: tokens })
        });
        const d = await r.json();
        if (r.ok && d.ok) { stage.value = "done"; }
        else vant.showToast(d.error || "提交失败，请稍后重试");
      } catch (e) { vant.showToast("网络异常，请稍后重试"); }
      submitting.value = false;
    }

    function goApp() { location.href = "/"; }

    onMounted(boot);

    return { FIELDS, stage, submitting, existsNick, photos, errors, form, area,
      provinces, cityList, countyList, onAreaProvince, onAreaCity,
      pickRadio, toggleMulti, toggleConfirm, onPickPhotos, removePhoto,
      onSubmit, goApp };
  }
}).use(vant).mount("#app");
