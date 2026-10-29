/**
 * 准点触发 GitHub Actions：GitHub 自带的定时触发高峰期会延迟数小时甚至丢失，
 * 由这个 Worker 在 wrangler.toml 配置的时间调用 workflow_dispatch。
 * 工作流里的 GitHub 定时保留作备份；带 auto=true 的触发和定时触发一样，当天已成功推送过就在 guard 里跳过。
 *
 * 密钥（wrangler secret put GITHUB_TOKEN）：fine-grained PAT，仅本仓库、Actions: Read and write
 */

// cron（UTC，要和 wrangler.toml 的 [triggers] 一致）→ 要触发的工作流
const SCHEDULES = {
  '27 0 * * *': 'agent-learning.yml', // 北京时间 08:27
  '7 1 * * *': 'daily-news.yml', // 北京时间 09:07
  '17 4 * * *': 'github-picks.yml', // 北京时间 12:17
};

export default {
  async scheduled(event, env) {
    const workflow = SCHEDULES[event.cron];
    if (!workflow) throw new Error(`没有配置 cron「${event.cron}」对应的工作流`);
    await dispatch(env, workflow);
  },
};

async function dispatch(env, workflow) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`;
  let error;
  for (let attempt = 1; attempt <= 3; attempt++) {
    if (attempt > 1) await new Promise((resolve) => setTimeout(resolve, 10_000));
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': 'news-bot-trigger',
        },
        body: JSON.stringify({ ref: 'main', inputs: { auto: 'true' } }),
      });
      if (res.ok) {
        console.log(`已触发 ${workflow}`);
        return;
      }
      error = new Error(`触发 ${workflow} 失败：${res.status} ${await res.text()}`);
      // 4xx 是令牌或参数问题，重试也没用
      if (res.status < 500) break;
    } catch (e) {
      error = e;
    }
    console.warn(`第 ${attempt} 次：${error.message}`);
  }
  throw error;
}
