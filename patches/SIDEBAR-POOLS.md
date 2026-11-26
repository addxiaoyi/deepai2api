# 把六条线的号池挂进主侧边栏（Open WebUI）

`LinePools.svelte` 已经给了"每条线一个独立页面"的完整视图（左栏 = 号池列表）。
想让它出现在 Open WebUI 的**主侧边栏**（跟 Chats / Workspace 并列），只需要在
fork 里改两处上游文件 —— 这两个文件属于 Open WebUI 本体，不在本 overlay 里，
所以以补丁片段的形式给出。

## 1. `src/lib/components/layout/Sidebar.svelte`

在 `<Sidebar>` 的导航列表里（`Chats` 那一组之后）插入：

```svelte
{#if $user?.role === 'admin'}
	<SidebarItem
		id="pools"
		href="/admin/settings/deepai#lines"
		icon="M3 5h18M3 12h18M3 19h18"
		label="号池"
		sublabel="{poolsTotal} 账号"
	/>
{/if}
```

其中 `poolsTotal` 在 `<script>` 顶部加：

```ts
import { getLinesOverview } from '$lib/apis/deepai';
import { token } from '$lib/stores';

let poolsTotal = 0;
onMount(async () => {
	try {
		const r = await getLinesOverview(token());
		poolsTotal = (r?.lines ?? []).reduce((sum, l) => sum + (l.stats?.total ?? 0), 0);
	} catch {}
});
```

## 2. 想要"真·每条线一个路由"

新建（本 overlay 已经给了组件，只要加薄路由）：

`src/routes/(app)/admin/pools/[line]/+page.svelte`

```svelte
<script lang="ts">
	import { page } from '$app/stores';
	import LinePools from '$lib/components/admin/Settings/LinePools.svelte';
</script>

<LinePools initial={$page.params.line} />
```

侧边栏条目把 `href` 换成 `/admin/pools/agnes`、`/admin/pools/cerebras` … 即可。

`LinePools.svelte` 目前用内部 state 记录当前线；要支持从路由进，
给它加一个可选 prop：

```ts
export let initial = '';
// onMount 里：if (initial) current = initial;
```

## 3. 不加路由也能用

`DeepAI` 设置页最底部就是 `<LinePools />`：左边一列是 14 条线（含原有的
cleverai / gabai / jevai / proxyscrape），点哪条右边就切到哪条线的独立页面。
所以即使不改上面任何一行，也已经"每条号池一个页面"，只是入口在设置页里。
