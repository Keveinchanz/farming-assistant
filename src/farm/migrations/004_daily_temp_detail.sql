-- 迁移 v4 —— daily_temp 补上最高/最低气温、降水与湿度
--
-- 为什么需要：7.1 的 daily_temp 只列了 tmean，但 7.3.4 的 11 个训练特征里
-- tmax_mean_30d / tmin_mean_30d / precip_sum_30d / hot_days_30d / cold_days_30d
-- 这五个都要用最高最低气温与降水。只存 tmean 的话，「我收了」在算特征时会直接
-- KeyError（这个错真的出现过），而且模型推理时也算不出这些特征。
--
-- 选择扩充表而不是「用的时候再联网取一次」：收获记录可能是在离线时点的，
-- 而特征必须与训练时用**同一份**数据算出来 —— 同一个地点不同时间取到的天气
-- 可能已经变过（ERA5 会回溯修订），那样训练集和推理就会悄悄飘开。
--
-- 列为 nullable：v1~v3 期间写入的老行没有这些值。读取方必须容忍缺失
-- （src/farm/harvest_model.py 的 build_features 就只统计有值的那些天）。

ALTER TABLE daily_temp ADD COLUMN tmax REAL;
ALTER TABLE daily_temp ADD COLUMN tmin REAL;
ALTER TABLE daily_temp ADD COLUMN precip REAL;
ALTER TABLE daily_temp ADD COLUMN humidity REAL;
