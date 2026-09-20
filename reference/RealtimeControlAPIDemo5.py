import json
import socket
import time

errorCode_Message = {
    "RALLY_ERROR_SUCCESS": "实时调控API结果成功",
    "RALLY_ERROR_INVALID_CHANNEL": "JSON串中指定的通道，在原刺激协议中不存在",
    "RALLY_ERROR_INVALID_STATUS": "下位机不处于 刺激中 状态",
    "RALLY_ERROR_INVALID_TDCS_PARAMETER": "JSON串中指定的tDCS类型通道的参数非法",
    "RALLY_ERROR_INVALID_TACS_PARAMETER": "JSON串中指定的tACS类型通道的参数非法",
    "RALLY_ERROR_INVALID_TOTAL_AMPTITUDE": "JSON串中设置的幅值 和 原刺激协议中的 组合结果超过幅值阈值",
    "RALLY_ERROR_INVALID_RETURN_CHANNEL_SETTING": "JSON串修改了原刺激协议中的Return通道",
    "RALLY_ERROR_INVALID_DEVICE_TYPE": "当前设备类型不是 便携式电刺激仪",
    "RALLY_ERROR_STOP_STIM": "立即停止刺激失败，请联系供应商",
    "RALLY_ERROR_SEND_PROTOCOL": "下发刺激协议失败，请联系供应商",
    "RALLY_ERROR_START_STIM": "开始刺激失败，请联系供应商",
    "RALLY_ERROR_INVALID_STIM_DURATION": "JSON串里的刺激总时长不合法（超过上限）",
    "RALLY_ERROR_INVALID_FLAT_RISING": "JSON串里的渐升时长不合法（超过上限）",
    "RALLY_ERROR_INVALID_FLAT_DECLINE": "JSON串里的渐降时长不合法（超过上限）",
    "RALLY_ERROR_INVALID_ORIGIN_PROTOCOL": "原始刺激协议不合法：包含了 tDCS 和 tACS 之外的类型",
    "RALLY_ERROR_INVALID_FIELD_STIM_TYPE": "JSON串中的刺激类型字段非法",
    "RALLY_ERROR_INVALID_FIELD_AMPITUDE": "JSON串中的幅值字段非法",
    "RALLY_ERROR_INVALID_FIELD_PHASE": "JSON串中的相位字段非法",
    "RALLY_ERROR_INVALID_FIELD_FREQUENCY": "JSON串中的频率字段非法",
    "RALLY_ERROR_INVALID_FIELD_DCBIAS": "JSON串中的直流偏置字段非法",
    "RALLY_ERROR_INVALID_FIELD_ELECTROD": "JSON串中的极性字段非法",
    "RALLY_ERROR_EXCEPTION_OCCURED": "响应实施调控时发生异常，请联系供应商",
    "RALLY_ERROR_TOTAL_AMPLITUDE_OVER_THRESHOLD": "用户设置的刺激幅度导致总幅度超过阈值",
    "RALLY_ERROR_INTERNAL_ERROR": "内部错误，详情需联系供应商",
}

# 实时调控的UDP命令的关键字
# UDP命令的开头必须是这个字符串
REALTIME_CONTROL_TOKEN = "RealTimeControl"


# 创建命令的JSON结构
# 此JSON结构的前提是：
#   ① Rally里使用的刺激协议中，刺激通道是 Fpz,Fp2,AF8,F8,FT8,C6,T8,TP8，返回通道随便是哪个
#   ② 上述8个通道的类型只能是 tDCS 或 tACS
def buildProtocol() -> str:
    protocolData = {}

    # 刺激时长
    protocolData['SD'] = 14005
    # 渐升
    protocolData['FR'] = 30
    # 渐降
    protocolData['FD'] = 30

    # 想要修改的通道参数
    # 刺激通道必须是当前正在使用的刺激协议中的刺激通道的子集，否则Rally会返回错误
    # 'N'：通道名   'T'：刺激类型   'A'：电流幅值  'F'：频率   'P';相位   'D':直流偏置
    protocolData['CHS'] = []

    # F5通道
    ch1 = {}
    ch1['N'] = "F5"
    ch1['T'] = "tA"
    ch1['A'] = 1.0
    ch1['F'] = 0.01
    ch1['P'] = 0
    ch1['D'] = 0

    # AF3通道
    ch2 = {}
    ch2['N'] = "AF3"
    ch2['T'] = "tA"
    ch2['A'] = 1.0
    ch2['F'] = 0.01
    ch2['P'] = 0
    ch2['D'] = 0

    # F1通道
    ch3 = {}
    ch3['N'] = "F1"
    ch3['T'] = "tA"
    ch3['A'] = 1.0
    ch3['F'] = 0.01
    ch3['P'] = 0
    ch3['D'] = 0

    # FC3通道
    ch4 = {}
    ch4['N'] = "FC3"
    ch4['T'] = "tA"
    ch4['A'] = 1.0
    ch4['F'] = 0.01
    ch4['P'] = 0
    ch4['D'] = 0

    # CZ通道
    ch5 = {}
    ch5['N'] = "CZ"
    ch5['T'] = "tA"
    ch5['A'] = 1.0
    ch5['F'] = 0.01
    ch5['P'] = 0
    ch5['D'] = 0

    # P3通道
    ch6 = {}
    ch6['N'] = "P3"
    ch6['T'] = "tA"
    ch6['A'] = 1.0
    ch6['F'] = 0.01
    ch6['P'] = 0
    ch6['D'] = 0

    # P4通道
    ch7 = {}
    ch7['N'] = "P4"
    ch7['T'] = "tA"
    ch7['A'] = 1.0
    ch7['F'] = 0.01
    ch7['P'] = 0
    ch7['D'] = 0

    # POZ通道
    ch8 = {}
    ch8['N'] = "POZ"
    ch8['T'] = "tA"
    ch8['A'] = 1.0
    ch8['F'] = 0.01
    ch8['P'] = 0
    ch8['D'] = 0

    # 将通道信息塞入整个JSON结构
    protocolData['CHS'].append(ch1)
    protocolData['CHS'].append(ch2)
    protocolData['CHS'].append(ch3)
    protocolData['CHS'].append(ch4)
    protocolData['CHS'].append(ch5)
    protocolData['CHS'].append(ch6)
    protocolData['CHS'].append(ch7)
    protocolData['CHS'].append(ch8)
    # 以字符串形式将JSON结构返回
    json_string = json.dumps(protocolData, indent=4)
    return json_string


if __name__ == "__main__":
    BUFSIZE = 1024

    # 构造JSON字符串
    json_string = buildProtocol()

    # 将 实施调控 的关键字作为头加上
    # 注意：要在关键字和真正的JSON串之间加一个 空格
    command = REALTIME_CONTROL_TOKEN + " " + json_string
    print(command)

    # 创建UDP socket
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ip_port = ('127.0.0.1', 8801)

    # 连续做 50 次调控
    for i in range(50):
        # 将 实施调控命令 发送给Rally程序

        client.sendto(command.encode('utf-8'), ip_port)

        # 等待Rally程序的应答
        data, server_addr = client.recvfrom(BUFSIZE)

        # 将Rally的应答输出
        message = str(data, encoding='utf-8')
        print(f'第 {i + 1} 次的处理结果: {errorCode_Message[message]}')

        # 两次调控间隔 5秒
        time.sleep(600)

    # 关闭UDP socket
    client.close()