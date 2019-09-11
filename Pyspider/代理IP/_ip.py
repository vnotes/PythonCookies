import asyncio
import re
import time
from asyncio.queues import PriorityQueue

import aiohttp
import chardet
import requests
from aiohttp.client_exceptions import ClientConnectionError
from aiohttp.client_exceptions import TimeoutError
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from selenium import webdriver

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

headers = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) ')}
loop = asyncio.get_event_loop()


class Proxy(object):
    def __init__(self, ip):
        self._url = 'http://' + ip
        self._score = 100

    @property
    def url(self):
        return self._url

    @property
    def score(self):
        return self._score

    def __lt__(self, other):
        '''
        由于优先队列是返回最小的，而这里分数高的代理优秀
        所以比较时反过来
        '''
        return self._score > other._score

    def success(self, time):
        self._score += int(10 / int(time + 1))

    def timeoutError(self):
        self._score -= 10

    def connectError(self):
        self._score -= 30

    def otherError(self):
        self._score -= 50


def getProxies():
    url = ("http://m.66ip.cn/mo.php?tqsl={proxy_number}")
    url = url.format(proxy_number=10)
    html = requests.get(url, headers=headers).content
    html = html.decode(chardet.detect(html)['encoding'])
    pattern = r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}'
    all_ip = re.findall(pattern, html)
    if len(all_ip) == 0:
        driver = webdriver.PhantomJS()
        driver.get(url)
        time.sleep(12)  # js等待5秒
        html = driver.page_source
        driver.quit()
        all_ip = re.findall(pattern, html)
    with open('66ip_' + str(time.time()), 'w', encoding='utf-8') as f:
        f.write(html)
    return all_ip


all_ip = set(getProxies()) | set(getProxies())
proxies = [Proxy(proxy) for proxy in all_ip]

mobike_url = "https://mwx.mobike.com/mobike-api/rent/nearbyBikesInfo.do"
data = {  # 请求参数: 纬度，经度！
    'latitude': '33.2',
    'longitude': '113.4',
}

headers = {
    'referer': "https://servicewechat.com/",
}


async def douban(proxy, session):
    try:
        start = time.time()
        async with session.post(mobike_url,
                                data=data,
                                proxy=proxy.url,
                                headers=headers,  # 可以引用到外部的headers
                                timeout=10) as resp:
            end = time.time()
            # print(resp.status)
            if resp.status == 200:
                proxy.success(end - start)
                print('%6.3d' % proxy._score, 'Used time-->', end - start, 's')
            else:
                proxy.otherError()
                print('*****', resp.status, '*****')
    except TimeoutError as te:
        print('%6.3d' % proxy._score, 'timeoutError')
        proxy.timeoutError()
    except ClientConnectionError as ce:
        print('%6.3d' % proxy._score, 'connectError')
        proxy.connectError()
    except Exception as e:
        print('%6.3d' % proxy._score, 'otherError->', e)
        proxy.otherError()


# ClientHttpProxyError

# TCPConnector维持链接池，限制并行连接的总量，当池满了，有请求退出再加入新请求，500和100相差不大
# ClientSession调用TCPConnector构造连接，Session可以共用
# Semaphore限制同时请求构造连接的数量，Semphore充足时，总时间与timeout差不多


async def initDouban():
    conn = aiohttp.TCPConnector(verify_ssl=False,
                                limit=100,  # 连接池在windows下不能太大, <500
                                use_dns_cache=True)
    tasks = []
    async with aiohttp.ClientSession(loop=loop, connector=conn) as session:
        for p in proxies:
            task = asyncio.ensure_future(douban(p, session))
            tasks.append(task)

        responses = asyncio.gather(*tasks)
        await responses
    conn.close()


def firstFilter():
    for i in range(2):
        s = time.time()
        future = asyncio.ensure_future(initDouban())
        loop.run_until_complete(future)
        e = time.time()
        print('----- init time %s-----\n' % i, e - s, 's')

    num = 0
    pq = PriorityQueue()
    for proxy in proxies:
        if proxy._score > 50:
            pq.put_nowait(proxy)
            num += 1
    print('原始ip数:%s' % len(all_ip), '; 筛选后:%s' % num)
    return pq


pq = firstFilter()


async def genDouban(sem, session):
    # Getter function with semaphore.
    while True:
        async with sem:
            proxy = await pq.get()
            await douban(proxy, session)
            await pq.put(proxy)


async def dynamicRunDouban(concurrency):
    '''
    TCPConnector维持链接池，限制并行连接的总量，当池满了，有请求退出再加入新请求
    ClientSession调用TCPConnector构造连接，Session可以共用
    Semaphore限制同时请求构造连接的数量，Semphore充足时，总时间与timeout差不多
    '''
    conn = aiohttp.TCPConnector(verify_ssl=False,
                                limit=concurrency,
                                use_dns_cache=True)
    tasks = []
    sem = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(loop=loop, connector=conn) as session:
        try:
            for i in range(concurrency):
                task = asyncio.ensure_future(genDouban(sem, session))
                tasks.append(task)

            responses = asyncio.gather(*tasks)
            await responses
        except KeyboardInterrupt:
            print('-----finishing-----\n')
            for task in tasks:
                task.cancel()
            if not conn.closed:
                conn.close()


future = asyncio.ensure_future(dynamicRunDouban(200))
loop.run_until_complete(future)

scores = [p.score for p in proxies]
scores.sort(reverse=True)
print('Most popular IPs:\n ------------\n', scores[:50],
      [i for i in scores if i > 100])
loop.is_closed()
