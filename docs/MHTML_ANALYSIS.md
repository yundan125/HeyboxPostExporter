# MHTML 样本分析

样本来源：`https://www.xiaoheihe.cn/app/bbs/link/187672249`，页面 meta 记录 website `1.1.14`、web version `3.0`。

## 帖子 DOM

- 帖子根节点：`.hb-bbs-image-text`
- 作者：`.link-section-user`
- 作者主页/UID：`.link-user__user-wrapper[href*="/app/user/profile/"]`
- 标题：`.section-title__content`
- 正文：`.image-text__content`
- 头图：`.image-text__header-image img`
- 标签/社区：`.content-tag-text`
- 时间/IP：`.link-section-link-data`

## 评论 DOM

- 评论区：`.link-comment`
- 一级评论：`.link-comment__list > .link-comment__comment-item`
- 一级评论 ID：`data-comment-id`
- 一级评论作者：`.info-box__username`
- 正文：`.comment-item__content`
- 点赞：`.like-box__cnt`
- 时间/IP：`.info-box__create-time` / `.info-box__ip`
- 楼中楼容器：`.link-comment__comment-children`
- 子回复：`.comment-children-item`
- 子回复 ID：`data-comment-id`
- 回复目标：`.children-item__reply-to`，文本形式为 `回复 用户名:`
- 展开按钮：`.comment-children__load-all`，样本文字包括“全部 N 条回复”“查看更多回复”

样本主 HTML 没有 `comment_id`、`user_id` 命名的 hydration JSON，但渲染后的 DOM 有稳定 `data-comment-id`，用户 UID 位于个人主页 URL。MHTML 包含头像、帖子图片和评论相关图片资源，但不包含原始 XHR 响应体。

## 样本计数

- 页面顶部“全部评论 104”：一级评论口径（API 字段 `total_floor_num`）。
- 页面底部“已有 221 条评论”：一级评论加楼中楼口径（API 字段 `link.comment_num`）。
- MHTML 实际含 36 个一级评论 DOM、48 个子回复 DOM，共 84 条。
- 有 9 个楼中楼仍存在展开按钮；因此该 MHTML 是部分加载快照，不是完整帖子归档。

## 前端接口取证

从页面同版本 JS bundle 和一次 Playwright 网络监听确认：

- `/bbs/app/link/tree`
  - `link_id`, `is_first`, `page`, `index=1`, `limit=20`, `owner_only=0`
  - `has_more_floors` 控制下一页。
- `/bbs/app/comment/sub/comments`
  - `root_comment_id`, `lastval=<最后一条 commentid>`
  - `has_more` 控制下一页。

网页请求基址为 `https://api.xiaoheihe.cn`。浏览器首次成功响应返回结构为 `result.link`、`result.comments[]`；每个一级评论包装为 `{ "comment": [一级评论, 子回复...] }`。页面自身会动态添加平台与风控参数，工具通过 Playwright 保留这一官方请求流程。
