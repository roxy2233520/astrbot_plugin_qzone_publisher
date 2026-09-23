"""QQ空间接口返回码与内部约定常量。"""

from http import HTTPStatus

# QQ空间业务返回码
QZONE_CODE_OK = 0
QZONE_CODE_UNKNOWN = -1
QZONE_CODE_LOGIN_EXPIRED = -3000
QZONE_CODE_IMAGE_EXPIRED = -100
# 解析层判定的「登录态失效 / 被风控」：接口没返回业务码，而是回了一整页 HTML 或
# 校验页面。用独立的合成码，便于传输层据此自动重取登录态后重试一次。
QZONE_CODE_LOGIN_REQUIRED = -3001
# 返回的是页面（JSONP / h5 框架页 / HTML），不是数据：多半是接口地址或参数不对，
# 重取登录态没有意义，因此**不触发**重登重试。
QZONE_CODE_UNEXPECTED_PAGE = -3002
# HTTP 403：请求被拒绝，常见原因是频率过高或参数不被接受，同样不盲目重登。
QZONE_CODE_FORBIDDEN = -3003
# 回复请求发出去了，但回查评论详情没找到自己的回复：无法确认是否成功，
# 不能据此判定登录失效，也不能当成成功（否则会重复回复）。
QZONE_CODE_REPLY_UNCONFIRMED = -3004
# 验证 / 风控页面：同样不是登录失效，重取登录态没有意义，因此单独分类。
QZONE_CODE_VERIFY_PAGE = -3005

# HTTP 状态码别名
HTTP_STATUS_UNAUTHORIZED = int(HTTPStatus.UNAUTHORIZED)
HTTP_STATUS_FORBIDDEN = int(HTTPStatus.FORBIDDEN)

# 传输层写入的元信息键，避免与业务字段冲突
QZONE_INTERNAL_META_KEY = "__qzone_internal__"
QZONE_INTERNAL_HTTP_STATUS_KEY = "http_status"

# 解析层产生的合成错误消息
QZONE_MSG_EMPTY_RESPONSE = "响应内容为空"
QZONE_MSG_INVALID_RESPONSE = "响应内容格式异常"
QZONE_MSG_JSON_PARSE_ERROR = "JSON 解析失败"
QZONE_MSG_NON_OBJECT_RESPONSE = "JSON 根节点不是对象"
QZONE_MSG_PERMISSION_DENIED = "权限不足"
# 判定为登录 / 风控页面时的可操作提示（写进回执，指引用 /空间重登 重取登录态）
QZONE_MSG_LOGIN_REQUIRED = (
    "登录态可能已失效或被风控拦截，请用 /空间重登 重取登录态，或稍后重试"
)
# 返回的是页面而不是数据：接口地址或参数不对，升级插件版本通常即可解决
QZONE_MSG_UNEXPECTED_PAGE = (
    "接口返回的是页面而不是数据（可能接口地址或参数不对），可先升级插件版本后重试"
)
# HTTP 403：不盲目重登，提示稍后重试
QZONE_MSG_FORBIDDEN = (
    "请求被拒绝（403）：可能是访问频率过高或接口参数不被接受，可稍后重试"
)
# 验证 / 风控页面：不是登录失效，重取登录态帮不上忙
QZONE_MSG_VERIFY_PAGE = (
    "接口返回的是验证 / 风控页面（未重取登录态），建议稍后重试或降低请求频率"
)
# 回复以「回查评论详情」为准：没查到自己的回复就无法确认，不能算成功
QZONE_MSG_REPLY_UNCONFIRMED = (
    "回复请求已发出，但回查评论详情没有找到自己的回复，暂时无法确认是否成功；"
    "可稍后到空间里确认，或调大巡检间隔后重试"
)
# 既不是 JSON、也不像登录 / 风控页面：只说明格式无法识别（响应片段已进日志）
QZONE_MSG_UNKNOWN_FORMAT = "响应格式无法识别（已记录响应片段，详见日志）"
