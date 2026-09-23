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
# 既不是 JSON、也不像登录 / 风控页面：只说明格式无法识别（响应片段已进日志）
QZONE_MSG_UNKNOWN_FORMAT = "响应格式无法识别（已记录响应片段，详见日志）"
